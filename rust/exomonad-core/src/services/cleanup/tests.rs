use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::domain::{BirthBranch, RoutingInfo, Slug};
use crate::services::agent_control::{
    finish_invocation, start_invocation, AgentResolver, AgentType, InvocationStatus,
    InvocationTrigger, Topology,
};
use crate::services::agent_resolver::AgentIdentityRecord;
use crate::services::git_worktree::GitWorktreeService;
use crate::services::mutex_registry::MutexRegistry;
use crate::services::repo::RepositoryIdentity;
use crate::services::tmux_ipc::{TmuxIpc, WindowId};
use std::path::Path;
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::Arc;
use wiremock::{matchers, Mock, MockServer, ResponseTemplate};

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

fn decision_for_pr(
    identity: &AgentIdentityRecord,
    repository: &RepositoryIdentity,
    pull_request: &CleanupPullRequest,
    merge_commit_reachable: Option<Result<bool, String>>,
) -> CleanupDecision {
    candidate_decision(DecisionContext {
        identity: Some(identity),
        identity_error: None,
        resolver_only: false,
        liveness: &CleanupLiveness::Dead,
        dirty: Some(false),
        protected: false,
        identity_drift: false,
        repository: Some(repository),
        repository_error: None,
        remote_error: None,
        pull_request: Some(pull_request),
        pr_error: None,
        head_matches_pull_request: None,
        remote_head_matches_pull_request: None,
        recovery_receipt: false,
        allow_no_pr: false,
        discard_dirty: false,
        merge_commit_reachable,
        target_error: None,
    })
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
        delete_remote_branch: false,
        allow_no_pr: false,
        discard_dirty: false,
    }
    .validate()
    .is_err());
    assert!(CleanupRequest {
        target: Some("agent".to_string()),
        sweep: true,
        apply: false,
        delete_remote_branch: false,
        allow_no_pr: false,
        discard_dirty: false,
    }
    .validate()
    .is_err());
}

#[test]
fn cleanup_overrides_require_named_apply_for_dirty_discard() {
    let request = CleanupRequest {
        allow_no_pr: true,
        ..CleanupRequest::default()
    };
    assert!(request.validate().is_err());

    let request = CleanupRequest {
        target: Some("abandoned-codex".to_string()),
        sweep: false,
        allow_no_pr: true,
        ..CleanupRequest::default()
    };
    assert!(request.validate().is_ok());

    let request = CleanupRequest {
        discard_dirty: true,
        ..request
    };
    assert!(request.validate().is_err());
    let request = CleanupRequest {
        apply: true,
        ..request
    };
    assert!(request.validate().is_ok());
}

#[test]
fn no_pr_override_only_bypasses_missing_pr_observation() {
    let identity = identity(Topology::WorktreePerAgent);
    let repository = RepositoryIdentity {
        owner: crate::domain::GithubOwner::try_from_str("owner").unwrap(),
        repo: crate::domain::GithubRepo::try_from_str("repo").unwrap(),
        base_branch: "main".to_string(),
        forge_host: "forgejo.test".to_string(),
        remote_url: "https://forgejo.test/owner/repo.git".to_string(),
        remote_name: "origin".to_string(),
    };
    let base = DecisionContext {
        identity: Some(&identity),
        identity_error: None,
        resolver_only: false,
        liveness: &CleanupLiveness::Dead,
        dirty: Some(false),
        protected: false,
        identity_drift: false,
        repository: Some(&repository),
        repository_error: None,
        remote_error: None,
        pull_request: None,
        pr_error: None,
        head_matches_pull_request: None,
        remote_head_matches_pull_request: None,
        merge_commit_reachable: None,
        target_error: None,
        recovery_receipt: false,
        allow_no_pr: true,
        discard_dirty: false,
    };
    assert!(candidate_decision(base.clone()).is_cleanable());

    let unavailable = DecisionContext {
        pr_error: Some("Forgejo unavailable"),
        ..base
    };
    assert!(!candidate_decision(unavailable).is_cleanable());
}

#[test]
fn dirty_worktree_requires_discard_authorization() {
    let identity = identity(Topology::SharedDir);
    let base = DecisionContext {
        identity: Some(&identity),
        identity_error: None,
        resolver_only: false,
        liveness: &CleanupLiveness::Dead,
        dirty: Some(true),
        protected: false,
        identity_drift: false,
        repository: None,
        repository_error: None,
        remote_error: None,
        pull_request: None,
        pr_error: None,
        head_matches_pull_request: None,
        remote_head_matches_pull_request: None,
        merge_commit_reachable: None,
        target_error: None,
        recovery_receipt: false,
        allow_no_pr: false,
        discard_dirty: false,
    };
    assert!(!candidate_decision(base.clone()).is_cleanable());
    assert!(candidate_decision(DecisionContext {
        discard_dirty: true,
        ..base
    })
    .is_cleanable());
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
        head_sha: Some("head".to_string()),
        merge_commit_sha: Some("merge".to_string()),
    };
    assert_eq!(
        candidate_decision(DecisionContext {
            identity: Some(&identity),
            identity_error: None,
            resolver_only: false,
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
            remote_head_matches_pull_request: None,
            recovery_receipt: false,
            allow_no_pr: false,
            discard_dirty: false,
            merge_commit_reachable: Some(Ok(true)),
            target_error: None,
        }),
        CleanupDecision::Cleanable
    );
    let mut wrong_base = pr.clone();
    wrong_base.base_ref = "release".to_string();
    assert!(!candidate_decision(DecisionContext {
        identity: Some(&identity),
        identity_error: None,
        resolver_only: false,
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
        remote_head_matches_pull_request: None,
        recovery_receipt: false,
        allow_no_pr: false,
        discard_dirty: false,
        merge_commit_reachable: Some(Ok(true)),
        target_error: None,
    })
    .is_cleanable());
    assert!(!decision_for_pr(&identity, &repo, &pr, Some(Ok(false))).is_cleanable());
    let mut missing_merge = pr;
    missing_merge.merge_commit_sha = None;
    assert!(!decision_for_pr(&identity, &repo, &missing_merge, None).is_cleanable());
}

#[test]
fn routing_target_absence_is_dead_but_probe_errors_are_unknown() {
    assert_eq!(classify_routing_target(Ok(false)), CleanupLiveness::Dead);
    assert_eq!(
        classify_routing_target(Err(anyhow::anyhow!("tmux unavailable"))),
        CleanupLiveness::Unknown
    );
}

#[tokio::test]
async fn terminal_invocation_with_stale_routing_requires_configured_tmux_session() {
    let temp = tempfile::tempdir().unwrap();
    let agent_dir = temp.path().join(".exo/agents/stale-codex");
    tokio::fs::create_dir_all(&agent_dir).await.unwrap();

    let invocation = start_invocation(
        &agent_dir,
        AgentType::Codex,
        InvocationTrigger::Spawn,
        RoutingInfo::window(WindowId::parse("@999999999").unwrap()),
        None,
        None,
    )
    .await
    .unwrap();
    finish_invocation(
        &agent_dir,
        &invocation.invocation_id,
        InvocationStatus::Exited,
        Some(0),
    )
    .await
    .unwrap();

    let service = VerifiedCleanupService::new(
        temp.path(),
        Arc::new(AgentResolver::load(temp.path().to_path_buf()).await),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
        None,
    );

    assert_eq!(service.liveness(&agent_dir).await, CleanupLiveness::Unknown);

    let configured_session = format!("cleanup-stale-{}", invocation.invocation_id);
    TmuxIpc::new_session(&configured_session, temp.path())
        .await
        .unwrap();
    let configured_service = VerifiedCleanupService::new(
        temp.path(),
        Arc::new(AgentResolver::load(temp.path().to_path_buf()).await),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
        Some(configured_session.clone()),
    );

    let liveness = configured_service.liveness(&agent_dir).await;
    TmuxIpc::kill_session(&configured_session).await.unwrap();

    assert_eq!(liveness, CleanupLiveness::Dead);
}

#[test]
fn services_propagate_configured_tmux_session_to_cleanup_service() {
    let mut services = crate::services::Services::test();
    services.tmux_session = Some("configured-cleanup-session".to_string());

    let cleanup_service = services.cleanup_service();

    assert_eq!(
        cleanup_service.tmux_session.as_deref(),
        Some("configured-cleanup-session")
    );
    assert!(Arc::ptr_eq(
        &cleanup_service.team_registry,
        &services.team_registry
    ));
    assert!(Arc::ptr_eq(
        &cleanup_service.supervisor_registry,
        &services.supervisor_registry
    ));
    assert!(Arc::ptr_eq(
        &cleanup_service.claude_session_registry,
        &services.claude_session_registry
    ));
}

#[test]
fn remote_branch_head_must_match_the_pull_request_head() {
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
        head_sha: Some("pr-head".to_string()),
        merge_commit_sha: Some("merge".to_string()),
    };
    assert!(!candidate_decision(DecisionContext {
        identity: Some(&identity),
        identity_error: None,
        resolver_only: false,
        liveness: &CleanupLiveness::Dead,
        dirty: Some(false),
        protected: false,
        identity_drift: false,
        repository: Some(&repo),
        repository_error: None,
        remote_error: None,
        pull_request: Some(&pr),
        pr_error: None,
        head_matches_pull_request: Some(true),
        remote_head_matches_pull_request: Some(false),
        recovery_receipt: false,
        allow_no_pr: false,
        discard_dirty: false,
        merge_commit_reachable: Some(Ok(true)),
        target_error: None,
    })
    .is_cleanable());
}

#[test]
fn duplicate_local_branches_are_refused() {
    let make_candidate = |id: &str| CleanupCandidate {
        id: id.to_string(),
        managed: true,
        resolver_only: false,
        recovery_receipt: false,
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
        dirty_evidence: None,
        protected: false,
        identity_drift: false,
        head_matches_pull_request: None,
        identity: Some(identity(Topology::WorktreePerAgent)),
        identity_error: None,
        remote_head_matches_pull_request: None,
        decision: CleanupDecision::Cleanable,
        branch: None,
        delete_remote_branch: false,
        allow_no_pr: false,
        discard_dirty: false,
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
        None,
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
        None,
    );
    let request = CleanupRequest {
        apply: true,
        delete_remote_branch: false,
        allow_no_pr: false,
        discard_dirty: false,
        ..CleanupRequest::default()
    };
    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Cleaned);
    assert!(!agent_dir.exists());
    let receipt_path = service
        .receipt_dir()
        .join(format!("{}.json", receipt.operation_id));
    let persisted: CleanupReceipt =
        serde_json::from_slice(&tokio::fs::read(receipt_path).await.unwrap()).unwrap();
    assert_eq!(persisted, receipt);
    let second = service.run(&request).await.unwrap();
    assert!(second.entries.is_empty());
}

#[tokio::test]
async fn apply_resumes_an_interrupted_resolver_only_cleanup() {
    let temp = tempfile::tempdir().unwrap();
    let agent_dir = temp.path().join(".exo/agents/stale-codex");
    tokio::fs::create_dir_all(&agent_dir).await.unwrap();
    let record = identity(Topology::SharedDir);
    tokio::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .await
    .unwrap();
    tokio::fs::write(agent_dir.join("exited_at"), "1")
        .await
        .unwrap();
    let resolver = Arc::new(AgentResolver::load(temp.path().to_path_buf()).await);
    let service = VerifiedCleanupService::new(
        temp.path(),
        resolver.clone(),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
        None,
    );
    let request = CleanupRequest {
        apply: true,
        delete_remote_branch: false,
        allow_no_pr: false,
        discard_dirty: false,
        ..CleanupRequest::default()
    };
    let plan = service.plan(&request).await.unwrap();
    let operation_id = "interrupted-cleanup";
    let mut interrupted = in_progress_receipt(&plan, 1);
    interrupted.operation_id = operation_id.to_string();
    interrupted.entries[0].actions = vec!["remove_agent_directory".to_string()];
    service.persist_receipt(&interrupted).await.unwrap();
    tokio::fs::remove_dir_all(&agent_dir).await.unwrap();
    let resumed_plan = service.plan(&request).await.unwrap();
    let resumed = service
        .resume_or_create_receipt(&resumed_plan, None, 2)
        .await
        .unwrap();
    assert_eq!(resumed.operation_id, operation_id);

    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.operation_id, operation_id);
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Cleaned);
    assert!(resolver.get(&record.agent_name).await.is_none());
}

#[tokio::test]
async fn receipt_persistence_failure_stops_before_resolver_deregistration() {
    let temp = tempfile::tempdir().unwrap();
    let agent_dir = temp.path().join(".exo/agents/stale-codex");
    tokio::fs::create_dir_all(&agent_dir).await.unwrap();
    let record = identity(Topology::SharedDir);
    tokio::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .await
    .unwrap();
    tokio::fs::write(agent_dir.join("exited_at"), "1")
        .await
        .unwrap();
    let resolver = Arc::new(AgentResolver::load(temp.path().to_path_buf()).await);
    let service = VerifiedCleanupService::new(
        temp.path(),
        resolver.clone(),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
        None,
    );
    service.fail_receipt_persist_on_call(4);
    let request = CleanupRequest {
        apply: true,
        ..CleanupRequest::default()
    };
    assert!(service.run(&request).await.is_err());
    assert!(!agent_dir.exists());
    assert!(resolver.get(&record.agent_name).await.is_some());

    service.fail_receipt_persist_on_call(0);
    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Cleaned);
    assert!(resolver.get(&record.agent_name).await.is_none());
}

#[tokio::test]
async fn receipt_persistence_failure_after_deregistration_is_recovered() {
    let temp = tempfile::tempdir().unwrap();
    let agent_dir = temp.path().join(".exo/agents/stale-codex");
    tokio::fs::create_dir_all(&agent_dir).await.unwrap();
    let record = identity(Topology::SharedDir);
    tokio::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .await
    .unwrap();
    tokio::fs::write(agent_dir.join("exited_at"), "1")
        .await
        .unwrap();
    let resolver = Arc::new(AgentResolver::load(temp.path().to_path_buf()).await);
    let service = VerifiedCleanupService::new(
        temp.path(),
        resolver.clone(),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
        None,
    );
    service.fail_receipt_persist_on_call(7);
    let request = CleanupRequest {
        apply: true,
        ..CleanupRequest::default()
    };
    assert!(service.run(&request).await.is_err());
    assert!(!agent_dir.exists());
    assert!(resolver.get(&record.agent_name).await.is_none());

    service.fail_receipt_persist_on_call(0);
    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Cleaned);
    assert!(receipt.entries[0]
        .actions
        .contains(&"deregister_identity".to_string()));
}

#[tokio::test]
async fn interrupted_cleanup_can_resume_by_slug_after_candidate_identity_changes() {
    let temp = tempfile::tempdir().unwrap();
    let agent_dir = temp.path().join(".exo/agents/stale-codex");
    tokio::fs::create_dir_all(&agent_dir).await.unwrap();
    let record = identity(Topology::SharedDir);
    tokio::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .await
    .unwrap();
    tokio::fs::write(agent_dir.join("exited_at"), "1")
        .await
        .unwrap();
    let resolver = Arc::new(AgentResolver::load(temp.path().to_path_buf()).await);
    let service = VerifiedCleanupService::new(
        temp.path(),
        resolver.clone(),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
        None,
    );
    let request = CleanupRequest {
        target: Some(record.slug.to_string()),
        sweep: false,
        apply: true,
        delete_remote_branch: false,
        allow_no_pr: false,
        discard_dirty: false,
    };
    let plan = service.plan(&request).await.unwrap();
    let mut interrupted = in_progress_receipt(&plan, 1);
    interrupted.operation_id = "slug-resume".to_string();
    interrupted.entries[0].candidate_id = "historical-worktree".to_string();
    service.persist_receipt(&interrupted).await.unwrap();
    tokio::fs::remove_dir_all(&agent_dir).await.unwrap();
    assert!(resolver.get(&record.agent_name).await.is_some());

    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.operation_id, "slug-resume");
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Cleaned);
    assert!(resolver.get(&record.agent_name).await.is_none());
}

#[tokio::test]
async fn resolver_identity_reuse_does_not_authorize_an_old_receipt() {
    let temp = tempfile::tempdir().unwrap();
    let agent_dir = temp.path().join(".exo/agents/stale-codex");
    tokio::fs::create_dir_all(&agent_dir).await.unwrap();
    let old_record = identity(Topology::SharedDir);
    tokio::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_vec(&old_record).unwrap(),
    )
    .await
    .unwrap();
    tokio::fs::write(agent_dir.join("exited_at"), "1")
        .await
        .unwrap();
    let resolver = Arc::new(AgentResolver::load(temp.path().to_path_buf()).await);
    let service = VerifiedCleanupService::new(
        temp.path(),
        resolver.clone(),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
        None,
    );
    let request = CleanupRequest {
        target: Some(old_record.agent_name.to_string()),
        sweep: false,
        apply: true,
        delete_remote_branch: false,
        allow_no_pr: false,
        discard_dirty: false,
    };
    let plan = service.plan(&request).await.unwrap();
    let mut interrupted = in_progress_receipt(&plan, 1);
    interrupted.operation_id = "identity-reuse".to_string();
    service.persist_receipt(&interrupted).await.unwrap();
    tokio::fs::remove_dir_all(&agent_dir).await.unwrap();

    let mut replacement = old_record.clone();
    replacement.slug = Slug::try_from_str("replacement").unwrap();
    replacement.birth_branch = BirthBranch::try_from_str("main.replacement").unwrap();
    replacement.working_dir = PathBuf::from(".exo/worktrees/replacement");
    replacement.topology = Topology::WorktreePerAgent;
    replacement.slice_id = Some("different-slice".to_string());
    resolver.register(replacement).await.unwrap();
    tokio::fs::remove_dir_all(&agent_dir).await.unwrap();

    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Refused);
    assert!(resolver.get(&old_record.agent_name).await.is_some());
}

#[tokio::test]
async fn resolver_only_cleanup_requires_an_in_progress_receipt() {
    let temp = tempfile::tempdir().unwrap();
    let agent_dir = temp.path().join(".exo/agents/stale-codex");
    tokio::fs::create_dir_all(&agent_dir).await.unwrap();
    let record = identity(Topology::WorktreePerAgent);
    tokio::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .await
    .unwrap();
    let resolver = Arc::new(AgentResolver::load(temp.path().to_path_buf()).await);
    tokio::fs::remove_dir_all(&agent_dir).await.unwrap();
    let service = VerifiedCleanupService::new(
        temp.path(),
        resolver.clone(),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
        None,
    );

    let plan = service.plan(&CleanupRequest::default()).await.unwrap();
    let candidate = &plan.candidates[0];
    assert!(candidate.resolver_only);
    assert_eq!(candidate.liveness, CleanupLiveness::Unknown);
    assert!(!candidate.decision.is_cleanable());

    let receipt = service
        .run(&CleanupRequest {
            apply: true,
            ..CleanupRequest::default()
        })
        .await
        .unwrap();
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Refused);
    assert!(resolver.get(&record.agent_name).await.is_some());
}

#[tokio::test]
async fn sweep_reports_malformed_identity_and_identityless_worktree() {
    let temp = tempfile::tempdir().unwrap();
    let malformed_dir = temp.path().join(".exo/agents/malformed");
    let worktree_dir = temp.path().join(".exo/worktrees/orphan");
    tokio::fs::create_dir_all(&malformed_dir).await.unwrap();
    tokio::fs::create_dir_all(&worktree_dir).await.unwrap();
    tokio::fs::write(malformed_dir.join("identity.json"), "{not-json")
        .await
        .unwrap();
    tokio::fs::write(malformed_dir.join("exited_at"), "1")
        .await
        .unwrap();

    let service = VerifiedCleanupService::new(
        temp.path(),
        Arc::new(AgentResolver::empty()),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
        None,
    );
    let plan = service.plan(&CleanupRequest::default()).await.unwrap();
    assert_eq!(plan.candidates.len(), 2);
    let ids: Vec<_> = plan
        .candidates
        .iter()
        .map(|candidate| candidate.id.as_str())
        .collect();
    assert!(ids.contains(&"malformed"));
    assert!(ids.contains(&"orphan"));
    assert!(plan.candidates.iter().all(|candidate| {
        !candidate.decision.is_cleanable() && candidate.identity_error.is_some()
    }));
}

#[tokio::test]
async fn real_worktree_is_discovered_from_authoritative_resolver_identity() {
    let temp = tempfile::tempdir().unwrap();
    run_git(temp.path(), &["init", "-q"]);
    run_git(
        temp.path(),
        &["config", "user.email", "cleanup@example.test"],
    );
    run_git(temp.path(), &["config", "user.name", "Cleanup Test"]);
    tokio::fs::write(temp.path().join("README"), "tracked\n")
        .await
        .unwrap();
    run_git(temp.path(), &["add", "README"]);
    run_git(temp.path(), &["commit", "-qm", "initial"]);
    run_git(temp.path(), &["branch", "-M", "main"]);
    run_git(temp.path(), &["branch", "main.stale"]);
    tokio::fs::create_dir_all(temp.path().join(".exo/worktrees"))
        .await
        .unwrap();
    run_git(
        temp.path(),
        &[
            "worktree",
            "add",
            "-q",
            ".exo/worktrees/stale-codex",
            "main.stale",
        ],
    );

    let agent_dir = temp.path().join(".exo/agents/stale-codex");
    tokio::fs::create_dir_all(&agent_dir).await.unwrap();
    let record = identity(Topology::WorktreePerAgent);
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
        None,
    );
    let plan = service.plan(&CleanupRequest::default()).await.unwrap();
    let candidate = plan
        .candidates
        .iter()
        .find(|candidate| candidate.id == "stale-codex")
        .unwrap();
    assert_eq!(candidate.identity.as_ref(), Some(&record));
    assert_eq!(
        candidate.worktree_path.as_deref(),
        Some(temp.path().join(".exo/worktrees/stale-codex").as_path())
    );
    assert_eq!(candidate.local_branch.as_deref(), Some("main.stale"));
    assert_eq!(candidate.dirty, Some(false));
    assert_eq!(candidate.issue, None);
    assert!(candidate.identity_error.is_none());
}

fn run_git(directory: &Path, args: &[&str]) {
    let output = Command::new("git")
        .current_dir(directory)
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git {:?} failed: {}",
        args,
        String::from_utf8_lossy(&output.stderr)
    );
}

fn git_stdout(directory: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .current_dir(directory)
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git {:?} failed: {}",
        args,
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

#[tokio::test]
async fn fetched_target_and_squash_merge_reachability_use_the_configured_remote() {
    let temp = tempfile::tempdir().unwrap();
    let remote = tempfile::tempdir().unwrap();
    run_git(remote.path(), &["init", "--bare", "-q"]);
    run_git(temp.path(), &["init", "-q"]);
    run_git(
        temp.path(),
        &["config", "user.email", "cleanup@example.test"],
    );
    run_git(temp.path(), &["config", "user.name", "Cleanup Test"]);
    tokio::fs::write(temp.path().join("README"), "initial\n")
        .await
        .unwrap();
    run_git(temp.path(), &["add", "README"]);
    run_git(temp.path(), &["commit", "-qm", "initial"]);
    run_git(temp.path(), &["branch", "-M", "main"]);
    run_git(
        temp.path(),
        &["remote", "add", "origin", remote.path().to_str().unwrap()],
    );
    run_git(temp.path(), &["push", "-q", "origin", "main"]);
    run_git(temp.path(), &["remote", "set-head", "origin", "main"]);
    run_git(temp.path(), &["checkout", "-qb", "main.stale"]);
    tokio::fs::write(temp.path().join("README"), "feature\n")
        .await
        .unwrap();
    run_git(temp.path(), &["commit", "-qam", "feature"]);
    run_git(temp.path(), &["checkout", "main"]);
    run_git(temp.path(), &["merge", "--squash", "main.stale"]);
    run_git(temp.path(), &["commit", "-qm", "squash"]);
    let merge = String::from_utf8_lossy(
        &Command::new("git")
            .current_dir(temp.path())
            .args(["rev-parse", "HEAD"])
            .output()
            .unwrap()
            .stdout,
    )
    .trim()
    .to_string();
    let branch_head = String::from_utf8_lossy(
        &Command::new("git")
            .current_dir(temp.path())
            .args(["rev-parse", "main.stale"])
            .output()
            .unwrap()
            .stdout,
    )
    .trim()
    .to_string();
    let ancestry = Command::new("git")
        .current_dir(temp.path())
        .args(["merge-base", "--is-ancestor", &branch_head, "main"])
        .output()
        .unwrap();
    assert!(
        !ancestry.status.success(),
        "squash merge must not preserve ancestry"
    );
    run_git(temp.path(), &["push", "-q", "origin", "main"]);
    let target = fetch_target_branch(temp.path(), "origin", "main")
        .await
        .unwrap();
    assert_eq!(target.branch, "main");
    assert!(merge_commit_reachable(temp.path(), &merge, &target)
        .await
        .unwrap());
}

#[tokio::test]
async fn remote_branch_deletion_uses_an_exact_expected_head_lease() {
    let temp = tempfile::tempdir().unwrap();
    let remote = tempfile::tempdir().unwrap();
    run_git(remote.path(), &["init", "--bare", "-q"]);
    run_git(temp.path(), &["init", "-q"]);
    run_git(
        temp.path(),
        &["config", "user.email", "cleanup@example.test"],
    );
    run_git(temp.path(), &["config", "user.name", "Cleanup Test"]);
    tokio::fs::write(temp.path().join("README"), "initial\n")
        .await
        .unwrap();
    run_git(temp.path(), &["add", "README"]);
    run_git(temp.path(), &["commit", "-qm", "initial"]);
    run_git(temp.path(), &["branch", "-M", "main"]);
    run_git(temp.path(), &["branch", "main.stale"]);
    run_git(
        temp.path(),
        &["remote", "add", "origin", remote.path().to_str().unwrap()],
    );
    run_git(temp.path(), &["push", "-q", "origin", "main.stale"]);
    let expected = local_branch_state(temp.path(), "main.stale")
        .await
        .unwrap()
        .unwrap();
    let conflicting = "0".repeat(expected.len());
    assert!(
        delete_remote_branch_with_lease(temp.path(), "origin", "main.stale", &conflicting,)
            .await
            .is_err()
    );
    assert_eq!(
        remote_branch_state(temp.path(), "origin", "main.stale")
            .await
            .unwrap()
            .as_deref(),
        Some(expected.as_str())
    );
    delete_remote_branch_with_lease(temp.path(), "origin", "main.stale", &expected)
        .await
        .unwrap();
    assert_eq!(
        remote_branch_state(temp.path(), "origin", "main.stale")
            .await
            .unwrap(),
        None
    );
}

#[tokio::test]
async fn local_branch_deletion_rejects_checked_out_worktrees_and_races() {
    let temp = tempfile::tempdir().unwrap();
    run_git(temp.path(), &["init", "-q"]);
    run_git(
        temp.path(),
        &["config", "user.email", "cleanup@example.test"],
    );
    run_git(temp.path(), &["config", "user.name", "Cleanup Test"]);
    tokio::fs::write(temp.path().join("README"), "initial\n")
        .await
        .unwrap();
    run_git(temp.path(), &["add", "README"]);
    run_git(temp.path(), &["commit", "-qm", "initial"]);
    run_git(temp.path(), &["branch", "-M", "main"]);
    run_git(temp.path(), &["branch", "main.stale"]);
    let expected = local_branch_state(temp.path(), "main.stale")
        .await
        .unwrap()
        .unwrap();
    run_git(temp.path(), &["checkout", "-q", "main.stale"]);
    tokio::fs::write(temp.path().join("README"), "advanced\n")
        .await
        .unwrap();
    run_git(temp.path(), &["add", "README"]);
    run_git(temp.path(), &["commit", "-qm", "advance"]);
    let advanced = local_branch_state(temp.path(), "main.stale")
        .await
        .unwrap()
        .unwrap();
    assert!(delete_local_branch(temp.path(), "main.stale", &advanced)
        .await
        .is_err());
    assert_eq!(
        local_branch_state(temp.path(), "main.stale").await.unwrap(),
        Some(advanced.clone())
    );
    run_git(temp.path(), &["checkout", "-q", "main"]);
    assert!(delete_local_branch(temp.path(), "main.stale", &expected)
        .await
        .is_err());
    assert_eq!(
        local_branch_state(temp.path(), "main.stale").await.unwrap(),
        Some(advanced.clone())
    );
    let linked = temp.path().join("linked-stale");
    let linked_arg = linked.to_str().unwrap().to_string();
    run_git(
        temp.path(),
        &["worktree", "add", "-q", linked_arg.as_str(), "main.stale"],
    );
    assert!(delete_local_branch(temp.path(), "main.stale", &advanced)
        .await
        .is_err());
    assert_eq!(
        local_branch_state(temp.path(), "main.stale").await.unwrap(),
        Some(advanced.clone())
    );
    run_git(
        temp.path(),
        &["worktree", "remove", "--force", linked_arg.as_str()],
    );
    delete_local_branch(temp.path(), "main.stale", &advanced)
        .await
        .unwrap();
    assert_eq!(
        local_branch_state(temp.path(), "main.stale").await.unwrap(),
        None
    );
    assert!(local_branch_state(temp.path(), "main")
        .await
        .unwrap()
        .is_some());
}

#[tokio::test]
async fn service_preserves_branch_checked_out_in_a_linked_worktree() {
    let temp = tempfile::tempdir().unwrap();
    let remote = tempfile::tempdir().unwrap();
    let remote_repo = remote.path().join("owner/repo.git");
    tokio::fs::create_dir_all(&remote_repo).await.unwrap();
    run_git(&remote_repo, &["init", "--bare", "-q"]);
    run_git(temp.path(), &["init", "-q"]);
    run_git(
        temp.path(),
        &["config", "user.email", "cleanup@example.test"],
    );
    run_git(temp.path(), &["config", "user.name", "Cleanup Test"]);
    tokio::fs::write(temp.path().join("README"), "initial\n")
        .await
        .unwrap();
    run_git(temp.path(), &["add", "README"]);
    run_git(temp.path(), &["commit", "-qm", "initial"]);
    run_git(temp.path(), &["branch", "-M", "main"]);
    run_git(temp.path(), &["checkout", "-qb", "main.stale"]);
    tokio::fs::write(temp.path().join("README"), "feature\n")
        .await
        .unwrap();
    run_git(temp.path(), &["commit", "-qam", "feature"]);
    run_git(temp.path(), &["checkout", "main"]);
    run_git(
        temp.path(),
        &["merge", "--no-ff", "-q", "-m", "merge stale", "main.stale"],
    );
    let merge_sha = git_stdout(temp.path(), &["rev-parse", "HEAD"]);
    let branch_sha = git_stdout(temp.path(), &["rev-parse", "main.stale"]);
    run_git(
        temp.path(),
        &["remote", "add", "origin", remote_repo.to_str().unwrap()],
    );
    run_git(temp.path(), &["push", "-q", "origin", "main", "main.stale"]);
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port().to_string();
    drop(listener);
    let mut daemon = Command::new("git")
        .args([
            "daemon",
            "--reuseaddr",
            "--export-all",
            &format!("--base-path={}", remote.path().display()),
            "--listen=127.0.0.1",
            &format!("--port={port}"),
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    let remote_url = format!("git://127.0.0.1:{port}/owner/repo.git");
    run_git(temp.path(), &["remote", "set-url", "origin", &remote_url]);
    run_git(
        temp.path(),
        &["fetch", "-q", "origin", "main", "main.stale"],
    );
    run_git(temp.path(), &["remote", "set-head", "origin", "main"]);
    let repository = crate::services::repo::get_repository_identity(temp.path())
        .await
        .expect("test repository identity must be resolvable");
    assert_eq!(repository.base_branch, "main");

    let server = MockServer::start().await;
    Mock::given(matchers::method("GET"))
        .and(matchers::path("/api/v1/repos/owner/repo/pulls"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(serde_json::json!([{
                "number": 1,
                "title": "Merged stale branch",
                "body": "",
                "state": "closed",
                "merged": true,
                "merge_commit_sha": merge_sha,
                "html_url": "http://forgejo.test/owner/repo/pulls/1",
                "head": {"ref": "main.stale", "sha": branch_sha},
                "base": {"ref": "main", "sha": merge_sha}
            }])),
        )
        .mount(&server)
        .await;

    let agent_dir = temp.path().join(".exo/agents/stale-codex");
    tokio::fs::create_dir_all(&agent_dir).await.unwrap();
    let record = identity(Topology::SharedDir);
    tokio::fs::write(
        agent_dir.join("identity.json"),
        serde_json::to_vec(&record).unwrap(),
    )
    .await
    .unwrap();
    let resolver = Arc::new(AgentResolver::load(temp.path().to_path_buf()).await);
    let service = VerifiedCleanupService::new(
        temp.path(),
        resolver,
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        Some(crate::services::forgejo::ForgejoClient::new(&server.uri(), "token").unwrap()),
        Arc::new(MutexRegistry::new()),
        None,
    );
    let pull_request = CleanupPullRequest {
        number: 1,
        head_ref: "main.stale".to_string(),
        base_ref: "main".to_string(),
        state: "closed".to_string(),
        merged: true,
        head_sha: Some(branch_sha.clone()),
        merge_commit_sha: Some(merge_sha.clone()),
    };
    let candidate = CleanupCandidate {
        id: "stale-codex".to_string(),
        managed: true,
        resolver_only: false,
        recovery_receipt: false,
        agent_name: record.agent_name.to_string(),
        issue: None,
        agent_dir,
        worktree_path: None,
        local_branch: Some("main.stale".to_string()),
        local_head_sha: Some(branch_sha.clone()),
        remote_branch: Some("main.stale".to_string()),
        remote_head_sha: Some(branch_sha.clone()),
        pull_request: Some(pull_request),
        liveness: CleanupLiveness::Dead,
        dirty: Some(false),
        dirty_evidence: None,
        protected: false,
        identity_drift: false,
        identity_error: None,
        head_matches_pull_request: Some(true),
        remote_head_matches_pull_request: Some(true),
        identity: Some(record),
        branch: Some(CleanupBranchEvidence {
            branch: Some("main.stale".to_string()),
            local_head_sha: Some(branch_sha.clone()),
            remote_name: Some("origin".to_string()),
            remote_branch: Some("main.stale".to_string()),
            remote_head_sha: Some(branch_sha.clone()),
            target_branch: Some("main".to_string()),
            target_head_sha: Some(merge_sha),
            merge_commit_reachable: Some(true),
            local: CleanupBranchAction {
                status: CleanupBranchActionStatus::WouldDelete,
                reason: None,
            },
            remote: CleanupBranchAction::default(),
        }),
        delete_remote_branch: false,
        allow_no_pr: false,
        discard_dirty: false,
        decision: CleanupDecision::Cleanable,
    };
    let linked = temp.path().join("linked-stale");
    let linked_arg = linked.to_str().unwrap().to_string();
    run_git(
        temp.path(),
        &["worktree", "add", "-q", linked_arg.as_str(), "main.stale"],
    );
    let mut receipt = CleanupReceipt {
        schema_version: CLEANUP_RECEIPT_SCHEMA_VERSION,
        operation_id: "linked-worktree".to_string(),
        plan_id: "linked-worktree-plan".to_string(),
        started_at: 0,
        finished_at: 0,
        dry_run: false,
        entries: vec![receipt_entry(
            &candidate,
            CleanupReceiptStatus::InProgress,
            Vec::new(),
            None,
        )],
    };
    let entry = service
        .execute_local_branch_action(&candidate, &mut receipt, 0)
        .await
        .expect("linked worktree must refuse local deletion");
    assert_eq!(entry.status, CleanupReceiptStatus::Refused);
    assert!(
        entry
            .reason
            .as_deref()
            .is_some_and(|reason| reason.contains("checked-out branch")),
        "unexpected refusal: {entry:?}"
    );
    assert_eq!(
        local_branch_state(temp.path(), "main.stale").await.unwrap(),
        Some(branch_sha)
    );
    run_git(
        temp.path(),
        &["worktree", "remove", "--force", linked_arg.as_str()],
    );
    daemon.kill().unwrap();
    daemon.wait().unwrap();
}
