use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::domain::{AgentName, BirthBranch, ClaudeSessionUuid, RoutingInfo, Slug, TeamName};
use crate::services::agent_control::{
    finish_invocation, start_invocation, AgentResolver, AgentType, InvocationStatus,
    InvocationTrigger, Topology,
};
use crate::services::agent_resolver::AgentIdentityRecord;
use crate::services::event_log::EventLog;
use crate::services::git_worktree::GitWorktreeService;
use crate::services::mutex_registry::MutexRegistry;
use crate::services::pr_registry::{publish_verified_head, PublicationProvenance, PublishedHead};
use crate::services::repo::RepositoryIdentity;
use crate::services::supervisor_registry::SupervisorInfo;
use crate::services::tmux_ipc::{TmuxIpc, WindowId};
use crate::services::{ForgejoClient, Services};
use std::os::unix::fs::PermissionsExt;
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
    assert!(!request.delete_remote_branch);
}

#[test]
fn cleanup_reason_is_bounded_and_round_trips() {
    let request = CleanupRequest {
        target: Some("abandoned-codex".to_string()),
        sweep: false,
        reason: Some("operator confirmed abandonment".to_string()),
        ..CleanupRequest::default()
    };
    assert!(request.validate().is_ok());

    let encoded = serde_json::to_value(&request).unwrap();
    let decoded: CleanupRequest = serde_json::from_value(encoded).unwrap();
    assert_eq!(decoded, request);

    let empty_reason = CleanupRequest {
        reason: Some("   ".to_string()),
        ..request.clone()
    };
    assert!(empty_reason.validate().is_err());

    let oversized_reason = CleanupRequest {
        reason: Some("x".repeat(1025)),
        ..request
    };
    assert!(oversized_reason.validate().is_err());
}

#[test]
fn target_validation_rejects_paths_and_ambiguous_requests() {
    assert!(CleanupRequest {
        target: Some("a/b".to_string()),
        sweep: false,
        apply: false,
        delete_remote_branch: false,
        reason: None,
        allow_no_pr: false,
        discard_dirty: false,
        preserve_unique_commits: false,
    }
    .validate()
    .is_err());
    assert!(CleanupRequest {
        target: Some("agent".to_string()),
        sweep: true,
        apply: false,
        delete_remote_branch: false,
        reason: None,
        allow_no_pr: false,
        discard_dirty: false,
        preserve_unique_commits: false,
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

    let request = CleanupRequest {
        delete_remote_branch: true,
        ..CleanupRequest::default()
    };
    assert!(request.validate().is_err());
    let request = CleanupRequest {
        target: Some("abandoned-codex".to_string()),
        sweep: false,
        delete_remote_branch: true,
        ..CleanupRequest::default()
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
fn recovered_closed_unmerged_pr_requires_recovered_provenance() {
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
        number: 7,
        head_ref: identity.birth_branch.to_string(),
        base_ref: "main".to_string(),
        state: "closed".to_string(),
        merged: false,
        head_sha: Some("head".to_string()),
        merge_commit_sha: None,
    };
    let context = DecisionContext {
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
        remote_head_matches_pull_request: Some(true),
        recovery_receipt: false,
        allow_no_pr: false,
        discard_dirty: false,
        merge_commit_reachable: None,
        target_error: None,
    };
    assert!(candidate_decision_for_recovery(context.clone(), true).is_cleanable());
    assert!(!candidate_decision(context).is_cleanable());
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

#[tokio::test]
async fn recovered_liveness_requires_current_server_tmux_evidence() {
    let temp = tempfile::tempdir().unwrap();
    let identity = identity(Topology::WorktreePerAgent);
    let resolver = Arc::new(AgentResolver::load(temp.path().to_path_buf()).await);
    let service = VerifiedCleanupService::new(
        temp.path(),
        resolver.clone(),
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
        None,
    );
    assert_eq!(
        service.recovered_liveness(&identity).await,
        CleanupLiveness::Unknown
    );

    let session = format!("cleanup-recovered-liveness-{}", std::process::id());
    let _ = TmuxIpc::kill_session(&session).await;
    TmuxIpc::new_session(&session, temp.path()).await.unwrap();
    let configured = VerifiedCleanupService::new(
        temp.path(),
        resolver,
        Arc::new(GitWorktreeService::new(temp.path().to_path_buf())),
        None,
        Arc::new(MutexRegistry::new()),
        Some(session.clone()),
    );
    assert_eq!(
        configured.recovered_liveness(&identity).await,
        CleanupLiveness::Dead
    );

    TmuxIpc::new(&session)
        .new_window(
            identity.display_name.as_str(),
            temp.path(),
            "sh",
            "sleep 30",
        )
        .await
        .unwrap();
    assert_eq!(
        configured.recovered_liveness(&identity).await,
        CleanupLiveness::Live
    );
    assert!(configured
        .revalidate_recovered_liveness(&identity)
        .await
        .is_err());
    TmuxIpc::kill_session(&session).await.unwrap();
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
        recovered_provenance: None,
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
        preserve_unique_commits: false,
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
        reason: None,
        allow_no_pr: false,
        discard_dirty: false,
        preserve_unique_commits: false,
        ..CleanupRequest::default()
    };
    let receipt = service.run(&request).await.unwrap();
    assert_eq!(
        receipt.entries[0].status,
        CleanupReceiptStatus::Cleaned,
        "receipt entry: {:?}",
        receipt.entries[0]
    );
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
        reason: None,
        allow_no_pr: false,
        discard_dirty: false,
        preserve_unique_commits: false,
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
        reason: None,
        allow_no_pr: false,
        discard_dirty: false,
        preserve_unique_commits: false,
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
        reason: None,
        allow_no_pr: false,
        discard_dirty: false,
        preserve_unique_commits: false,
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

struct RealCleanupFixture {
    _temp: tempfile::TempDir,
    _forgejo: MockServer,
    services: Services,
    record: AgentIdentityRecord,
    agent_dir: PathBuf,
    worktree: PathBuf,
}

async fn real_cleanup_fixture() -> RealCleanupFixture {
    let temp = tempfile::tempdir().unwrap();
    let project = temp.path().join("project");
    let remote = temp.path().join("owner/repo.git");
    tokio::fs::create_dir_all(&project).await.unwrap();
    tokio::fs::create_dir_all(&remote).await.unwrap();
    run_git(&remote, &["init", "--bare", "-q"]);
    tokio::fs::write(remote.join("git-daemon-export-ok"), "")
        .await
        .unwrap();
    run_git(&project, &["init", "-q"]);
    run_git(&project, &["config", "user.email", "cleanup@example.test"]);
    run_git(&project, &["config", "user.name", "Cleanup Test"]);
    tokio::fs::write(project.join("README"), "initial\n")
        .await
        .unwrap();
    run_git(&project, &["add", "README"]);
    run_git(&project, &["commit", "-qm", "initial"]);
    run_git(&project, &["branch", "-M", "main"]);
    run_git(&project, &["branch", "main.stale"]);
    run_git(&project, &["checkout", "-q", "main.stale"]);
    tokio::fs::write(project.join("README"), "abandoned commit\n")
        .await
        .unwrap();
    run_git(&project, &["commit", "-qam", "abandoned unique commit"]);
    run_git(&project, &["checkout", "-q", "main"]);
    run_git(
        &project,
        &["remote", "add", "origin", remote.to_str().unwrap()],
    );
    run_git(&project, &["push", "-q", "origin", "main", "main.stale"]);
    run_git(&project, &["remote", "set-head", "origin", "main"]);

    let ssh_command = temp.path().join("local-ssh");
    let ssh_script = format!(
        "#!/bin/sh\ncase \"${{2:-}}\" in\n  git-upload-pack*) exec git-upload-pack '{}' ;;\n  git-receive-pack*) exec git-receive-pack '{}' ;;\n  *) exit 1 ;;\nesac\n",
        remote.display(),
        remote.display()
    );
    tokio::fs::write(&ssh_command, ssh_script).await.unwrap();
    tokio::fs::set_permissions(&ssh_command, std::fs::Permissions::from_mode(0o755))
        .await
        .unwrap();
    run_git(
        &project,
        &["config", "core.sshCommand", ssh_command.to_str().unwrap()],
    );
    let remote_url = "git@forgejo.test:owner/repo.git";
    run_git(&project, &["remote", "set-url", "origin", remote_url]);

    tokio::fs::create_dir_all(project.join(".exo/worktrees"))
        .await
        .unwrap();
    run_git(
        &project,
        &[
            "worktree",
            "add",
            "-q",
            ".exo/worktrees/stale-codex",
            "main.stale",
        ],
    );
    let worktree = project.join(".exo/worktrees/stale-codex");
    tokio::fs::write(worktree.join("README"), "dirty\n")
        .await
        .unwrap();
    tokio::fs::write(worktree.join("abandoned.txt"), "untracked\n")
        .await
        .unwrap();

    let agent_dir = project.join(".exo/agents/stale-codex");
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

    let forgejo_server = MockServer::start().await;
    Mock::given(matchers::method("GET"))
        .and(matchers::path("/api/v1/repos/owner/repo/pulls"))
        .respond_with(ResponseTemplate::new(200).set_body_json(Vec::<String>::new()))
        .mount(&forgejo_server)
        .await;
    let mut services = Services::test();
    services.project_dir = project.clone();
    services.forgejo_client =
        Some(ForgejoClient::new(&forgejo_server.uri(), "test-token").unwrap());
    services.agent_resolver = Arc::new(AgentResolver::load(project.clone()).await);
    services.git_wt = Arc::new(GitWorktreeService::new(project));
    services
        .claude_session_registry
        .register(
            record.agent_name.as_str(),
            ClaudeSessionUuid::try_from_str("uuid-123").unwrap(),
        )
        .await;
    services
        .supervisor_registry
        .register(
            &[record.birth_branch.to_string()],
            SupervisorInfo {
                supervisor: AgentName::try_from_str("root").unwrap(),
                team: TeamName::try_from_str("cleanup-team").unwrap(),
            },
        )
        .await;
    RealCleanupFixture {
        _temp: temp,
        _forgejo: forgejo_server,
        services,
        record,
        agent_dir,
        worktree,
    }
}

#[tokio::test]
async fn recovered_merged_pr_residual_uses_verified_provenance_end_to_end() {
    let mut fixture = real_cleanup_fixture().await;
    let project = fixture.services.project_dir.clone();
    let branch = fixture.record.birth_branch.to_string();
    let tmux_session = format!("cleanup-recovery-{}", std::process::id());
    let _ = TmuxIpc::kill_session(&tmux_session).await;
    TmuxIpc::new_session(&tmux_session, &project).await.unwrap();
    fixture.services.tmux_session = Some(tmux_session.clone());
    let branch_sha = local_branch_state(&project, &branch)
        .await
        .unwrap()
        .unwrap();
    run_git(
        &project,
        &[
            "worktree",
            "remove",
            "--force",
            fixture.worktree.to_str().unwrap(),
        ],
    );
    run_git(&project, &["worktree", "prune", "--expire", "now"]);
    tokio::fs::create_dir_all(fixture.worktree.join(".exo/runtime"))
        .await
        .unwrap();
    tokio::fs::remove_dir_all(&fixture.agent_dir).await.unwrap();
    fixture
        .services
        .agent_resolver
        .deregister(&fixture.record.agent_name)
        .await
        .unwrap();
    run_git(
        &project,
        &["merge", "--no-ff", "-qm", "merge recovered PR", &branch],
    );
    run_git(&project, &["push", "-q", "origin", "main"]);
    let merge_sha = local_branch_state(&project, "main").await.unwrap().unwrap();

    fixture._forgejo.reset().await;
    Mock::given(matchers::method("GET"))
        .and(matchers::path("/api/v1/repos/owner/repo/pulls"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(serde_json::json!([{
                "number": 43,
                "title": "Recovered merged branch",
                "body": "",
                "state": "closed",
                "merged": true,
                "merge_commit_sha": merge_sha,
                "html_url": "http://forgejo.test/owner/repo/pulls/43",
                "head": {"ref": branch, "sha": branch_sha},
                "base": {"ref": "main", "sha": merge_sha}
            }])),
        )
        .mount(&fixture._forgejo)
        .await;

    let event_log = EventLog::open(project.join(".exo/events")).unwrap();
    event_log
        .append(
            "agent.spawned",
            "root",
            &serde_json::json!({
                "child_agent": "stale-codex",
                "agent_type": "codex",
                "branch": branch,
                "topology": "worktreeperagent"
            }),
        )
        .unwrap();
    event_log
        .append(
            "pr.published",
            "stale-codex",
            &serde_json::json!({
                "agent_id": "stale-codex",
                "pr_number": 43,
                "head_branch": branch,
                "base_branch": "main",
                "head_sha": branch_sha
            }),
        )
        .unwrap();
    event_log
        .append(
            "agent.invocation.finished",
            "stale-codex",
            &serde_json::json!({
                "invocation_id": "inv-43",
                "slice_id": "slice-43",
                "outcome": "finished",
                "status": "exited",
                "branch": branch,
                "head_sha": branch_sha,
                "pr_number": 43
            }),
        )
        .unwrap();
    publish_verified_head(
        &project,
        PublishedHead {
            pr_number: 43,
            head_branch: branch.clone(),
            base_branch: "main".to_string(),
            head_sha: branch_sha.clone(),
            author_agent: Some("stale-codex".to_string()),
            author_role: Some("dev".to_string()),
            provenance: PublicationProvenance::LedgerOwned,
            slice_id: Some("slice-43".to_string()),
            invocation_id: Some("inv-43".to_string()),
            invocation_trigger: Some("spawn".to_string()),
            invocation_runtime: Some("codex".to_string()),
            invocation_succession: Vec::new(),
        },
    )
    .await
    .unwrap();
    fixture.services.event_log = Some(Arc::new(event_log));

    let service = fixture.services.cleanup_service();
    let request = CleanupRequest {
        target: Some(fixture.record.agent_name.to_string()),
        sweep: false,
        apply: true,
        delete_remote_branch: true,
        ..CleanupRequest::default()
    };
    let missing_metadata_plan = service.plan(&request).await.unwrap();
    assert!(!missing_metadata_plan.candidates[0].decision.is_cleanable());

    fixture._forgejo.reset().await;
    Mock::given(matchers::method("GET"))
        .and(matchers::path("/api/v1/repos/owner/repo/pulls"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(serde_json::json!([{
                "number": 43,
                "title": "Recovered merged branch",
                "body": format!(
                    "Authoring-Agent: {}\nBirth-Branch: {branch}\n",
                    fixture.record.agent_name
                ),
                "state": "closed",
                "merged": false,
                "merge_commit_sha": null,
                "html_url": "http://forgejo.test/owner/repo/pulls/43",
                "head": {"ref": branch, "sha": branch_sha},
                "base": {"ref": "main", "sha": "base-sha"}
            }])),
        )
        .mount(&fixture._forgejo)
        .await;
    let closed_unmerged_plan = service.plan(&request).await.unwrap();
    assert_eq!(
        closed_unmerged_plan.candidates[0].decision,
        CleanupDecision::Cleanable
    );
    assert_eq!(
        closed_unmerged_plan.candidates[0]
            .pull_request
            .as_ref()
            .map(|pull_request| pull_request.merged),
        Some(false)
    );
    let revalidated_closed_unmerged = service
        .revalidate_branch(&closed_unmerged_plan.candidates[0])
        .await
        .unwrap();
    assert_eq!(
        revalidated_closed_unmerged.branch,
        closed_unmerged_plan.candidates[0]
            .branch
            .as_ref()
            .and_then(|branch| branch.branch.clone())
            .unwrap()
    );

    fixture._forgejo.reset().await;
    Mock::given(matchers::method("GET"))
        .and(matchers::path("/api/v1/repos/owner/repo/pulls"))
        .respond_with(
            ResponseTemplate::new(200).set_body_json(serde_json::json!([{
                "number": 43,
                "title": "Recovered merged branch",
                "body": format!(
                    "Authoring-Agent: {}\nBirth-Branch: {branch}\n",
                    fixture.record.agent_name
                ),
                "state": "closed",
                "merged": true,
                "merge_commit_sha": merge_sha,
                "html_url": "http://forgejo.test/owner/repo/pulls/43",
                "head": {"ref": branch, "sha": branch_sha},
                "base": {"ref": "main", "sha": merge_sha}
            }])),
        )
        .mount(&fixture._forgejo)
        .await;
    let plan = service.plan(&request).await.unwrap();
    let candidate = &plan.candidates[0];
    assert_eq!(candidate.decision, CleanupDecision::Cleanable);
    assert_eq!(candidate.local_branch.as_deref(), Some(branch.as_str()));
    assert_eq!(
        candidate.pull_request.as_ref().map(|pr| pr.number),
        Some(43)
    );
    assert_eq!(
        candidate
            .branch
            .as_ref()
            .and_then(|branch| branch.branch.as_deref()),
        Some(branch.as_str())
    );
    assert!(candidate
        .recovered_provenance
        .as_ref()
        .is_some_and(|evidence| evidence.identity_sources.len() >= 3));

    let mut liveness_race = service
        .resume_or_create_receipt(&plan, request.target.as_deref(), 1)
        .await
        .unwrap();
    let live_window = TmuxIpc::new(&tmux_session)
        .new_window(
            candidate.identity.as_ref().unwrap().display_name.as_str(),
            &project,
            "sh",
            "sleep 30",
        )
        .await
        .unwrap();
    let race_entry = service
        .execute_remote_branch_action(candidate, &mut liveness_race, 0)
        .await
        .expect("live recovered target must refuse before remote mutation");
    assert_eq!(race_entry.status, CleanupReceiptStatus::Refused);
    assert!(race_entry
        .reason
        .as_deref()
        .is_some_and(|reason| reason.contains("live") || reason.contains("liveness")));
    assert!(remote_branch_state(&project, "origin", &branch)
        .await
        .unwrap()
        .is_some());
    TmuxIpc::new(&tmux_session)
        .kill_window(&live_window)
        .await
        .unwrap();

    let mut interrupted = service
        .resume_or_create_receipt(&plan, request.target.as_deref(), 1)
        .await
        .unwrap();
    assert!(service
        .execute_remote_branch_action(candidate, &mut interrupted, 0)
        .await
        .is_none());
    interrupted.entries[0].status = CleanupReceiptStatus::Failed;
    interrupted.entries[0].reason = Some("simulated downstream cleanup failure".to_string());
    service.persist_receipt(&interrupted).await.unwrap();
    tokio::fs::remove_dir_all(&fixture.worktree).await.unwrap();
    let recovered_resources = service.discover_resources(&request).await.unwrap();
    assert!(recovered_resources.iter().any(|resource| {
        resource.id == fixture.record.agent_name.as_str()
            && resource.recovered_provenance.is_some()
            && resource.worktree_path.as_ref() == Some(&fixture.worktree)
    }));

    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Cleaned);
    assert!(receipt.entries[0].recovered_provenance.is_some());
    assert!(receipt.entries[0]
        .actions
        .contains(&"worktree_already_absent".to_string()));
    assert!(receipt.entries[0]
        .actions
        .contains(&"delete_remote_branch".to_string()));
    assert!(!fixture.worktree.exists());
    assert!(!fixture.agent_dir.exists());
    assert!(local_branch_state(&project, &branch)
        .await
        .unwrap()
        .is_none());
    assert!(remote_branch_state(&project, "origin", &branch)
        .await
        .unwrap()
        .is_none());
    let second = service.apply(&request).await.unwrap();
    assert!(second.entries.is_empty());
    TmuxIpc::kill_session(&tmux_session).await.unwrap();
}

#[tokio::test]
async fn dirty_no_pr_cleanup_removes_resources_registries_and_is_idempotent() {
    let fixture = real_cleanup_fixture().await;
    let service = fixture.services.cleanup_service();
    let request = CleanupRequest {
        target: Some(fixture.record.agent_name.to_string()),
        sweep: false,
        apply: true,
        delete_remote_branch: false,
        reason: Some("confirmed abandoned dirty work".to_string()),
        allow_no_pr: true,
        discard_dirty: true,
        preserve_unique_commits: false,
    };

    let plan = service.plan(&request).await.unwrap();
    let candidate = plan
        .candidates
        .iter()
        .find(|candidate| candidate.id == fixture.record.agent_name.as_str())
        .unwrap();
    assert_eq!(candidate.decision, CleanupDecision::Cleanable);
    assert_eq!(candidate.dirty, Some(true));
    assert!(candidate
        .dirty_evidence
        .as_ref()
        .unwrap()
        .tracked_paths
        .contains(&"README".to_string()));
    assert!(candidate
        .dirty_evidence
        .as_ref()
        .unwrap()
        .untracked_paths
        .contains(&"abandoned.txt".to_string()));
    assert_eq!(
        candidate.branch.as_ref().unwrap().local.status,
        CleanupBranchActionStatus::WouldDelete
    );

    let receipt = service.run(&request).await.unwrap();
    let entry = &receipt.entries[0];
    assert_eq!(
        entry.status,
        CleanupReceiptStatus::Cleaned,
        "cleanup entry: {entry:?}"
    );
    assert!(entry.actions.contains(&"allow_no_pr_override".to_string()));
    assert!(entry.actions.contains(&"record_dirty_evidence".to_string()));
    assert!(entry.actions.contains(&"discard_dirty_changes".to_string()));
    assert!(entry.actions.contains(&"remove_worktree".to_string()));
    assert!(entry
        .actions
        .contains(&"remove_agent_directory".to_string()));
    assert!(entry
        .actions
        .contains(&"remove_ephemeral_registrations".to_string()));
    assert!(!fixture.worktree.exists());
    assert!(!fixture.agent_dir.exists());
    assert!(
        local_branch_state(fixture.services.project_dir.as_path(), "main.stale")
            .await
            .unwrap()
            .is_none()
    );
    assert!(remote_branch_state(
        fixture.services.project_dir.as_path(),
        "origin",
        "main.stale"
    )
    .await
    .unwrap()
    .is_some());
    assert!(fixture
        .services
        .agent_resolver
        .get(&fixture.record.agent_name)
        .await
        .is_none());
    assert!(fixture
        .services
        .claude_session_registry
        .get(fixture.record.agent_name.as_str())
        .await
        .is_none());
    assert!(fixture
        .services
        .supervisor_registry
        .lookup(fixture.record.birth_branch.as_str())
        .await
        .is_none());

    let second = service.run(&request).await.unwrap();
    assert!(second.entries.is_empty());
}

#[tokio::test]
async fn dirty_no_pr_cleanup_recovers_after_receipt_failure() {
    let fixture = real_cleanup_fixture().await;
    let service = fixture.services.cleanup_service();
    let request = CleanupRequest {
        target: Some(fixture.record.agent_name.to_string()),
        sweep: false,
        apply: true,
        allow_no_pr: true,
        discard_dirty: true,
        ..CleanupRequest::default()
    };

    service.fail_receipt_persist_on_call(4);
    assert!(service.run(&request).await.is_err());
    service.fail_receipt_persist_on_call(0);
    let receipt = service.run(&request).await.unwrap();

    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Cleaned);
    assert!(!fixture.worktree.exists());
    assert!(!fixture.agent_dir.exists());
    assert!(fixture
        .services
        .agent_resolver
        .get(&fixture.record.agent_name)
        .await
        .is_none());
}

#[tokio::test]
async fn remote_deletion_revalidates_worktree_at_mutation_boundary() {
    let fixture = real_cleanup_fixture().await;
    let service = fixture.services.cleanup_service();
    let request = CleanupRequest {
        target: Some(fixture.record.agent_name.to_string()),
        sweep: false,
        apply: true,
        delete_remote_branch: true,
        allow_no_pr: true,
        discard_dirty: true,
        ..CleanupRequest::default()
    };
    let plan = service.plan(&request).await.unwrap();
    let candidate = &plan.candidates[0];
    assert_eq!(candidate.decision, CleanupDecision::Cleanable);
    let mut receipt = in_progress_receipt(&plan, 1);

    tokio::fs::write(
        fixture.worktree.join("late-change.txt"),
        "changed after planning",
    )
    .await
    .unwrap();
    let entry = service
        .execute_remote_branch_action(candidate, &mut receipt, 0)
        .await
        .expect("late worktree mutation must refuse remote deletion");

    assert_eq!(entry.status, CleanupReceiptStatus::Refused);
    assert!(entry
        .reason
        .as_deref()
        .is_some_and(|reason| { reason.contains("dirty worktree changed since planning") }));
    assert!(remote_branch_state(
        fixture.services.project_dir.as_path(),
        "origin",
        "main.stale"
    )
    .await
    .unwrap()
    .is_some());
}

#[tokio::test]
async fn remote_deletion_revalidates_liveness_at_mutation_boundary() {
    let fixture = real_cleanup_fixture().await;
    let service = fixture.services.cleanup_service();
    let request = CleanupRequest {
        target: Some(fixture.record.agent_name.to_string()),
        sweep: false,
        apply: true,
        delete_remote_branch: true,
        allow_no_pr: true,
        discard_dirty: true,
        ..CleanupRequest::default()
    };
    let plan = service.plan(&request).await.unwrap();
    let candidate = &plan.candidates[0];
    let mut receipt = in_progress_receipt(&plan, 1);
    start_invocation(
        &fixture.agent_dir,
        AgentType::Codex,
        InvocationTrigger::Spawn,
        RoutingInfo::window(WindowId::parse("@999999999").unwrap()),
        None,
        None,
    )
    .await
    .unwrap();

    let entry = service
        .execute_remote_branch_action(candidate, &mut receipt, 0)
        .await
        .expect("late liveness change must refuse remote deletion");

    assert_eq!(entry.status, CleanupReceiptStatus::Refused);
    assert!(entry
        .reason
        .as_deref()
        .is_some_and(|reason| reason.contains("no longer provably dead")));
    assert!(remote_branch_state(
        fixture.services.project_dir.as_path(),
        "origin",
        "main.stale"
    )
    .await
    .unwrap()
    .is_some());
}

#[tokio::test]
async fn service_remote_deletion_refuses_a_head_race_at_the_exact_lease() {
    let fixture = real_cleanup_fixture().await;
    let service = fixture.services.cleanup_service();
    let request = CleanupRequest {
        target: Some(fixture.record.agent_name.to_string()),
        sweep: false,
        apply: true,
        delete_remote_branch: true,
        allow_no_pr: true,
        discard_dirty: true,
        ..CleanupRequest::default()
    };
    let plan = service.plan(&request).await.unwrap();
    let candidate = &plan.candidates[0];
    let mut receipt = in_progress_receipt(&plan, 1);
    let remote_repo = fixture._temp.path().join("owner/repo.git");
    let hook = fixture.services.project_dir.join(".git/hooks/pre-push");
    let hook_contents = format!(
        "#!/bin/sh\ngit --git-dir='{}' update-ref refs/heads/main.stale refs/heads/main\nexit 0\n",
        remote_repo.display()
    );
    tokio::fs::write(&hook, hook_contents).await.unwrap();
    tokio::fs::set_permissions(&hook, std::fs::Permissions::from_mode(0o755))
        .await
        .unwrap();

    let entry = service
        .execute_remote_branch_action(candidate, &mut receipt, 0)
        .await
        .expect("remote head movement must refuse the exact lease");

    assert_eq!(entry.status, CleanupReceiptStatus::Refused);
    assert!(entry
        .reason
        .as_deref()
        .is_some_and(|reason| reason.contains("expected head")));
    let moved_head = local_branch_state(fixture.services.project_dir.as_path(), "main")
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        remote_branch_state(
            fixture.services.project_dir.as_path(),
            "origin",
            "main.stale"
        )
        .await
        .unwrap(),
        Some(moved_head)
    );
}

#[tokio::test]
async fn remote_success_resumes_from_a_failed_downstream_receipt() {
    let fixture = real_cleanup_fixture().await;
    let service = fixture.services.cleanup_service();
    let request = CleanupRequest {
        target: Some(fixture.record.agent_name.to_string()),
        sweep: false,
        apply: true,
        delete_remote_branch: true,
        allow_no_pr: true,
        discard_dirty: true,
        ..CleanupRequest::default()
    };

    service.fail_receipt_persist_on_call(6);
    assert!(service.run(&request).await.is_err());
    service.fail_receipt_persist_on_call(0);
    assert!(remote_branch_state(
        fixture.services.project_dir.as_path(),
        "origin",
        "main.stale"
    )
    .await
    .unwrap()
    .is_none());

    let plan = service.plan(&request).await.unwrap();
    let mut failed = service
        .resume_or_create_receipt(&plan, request.target.as_deref(), 1)
        .await
        .unwrap();
    assert_eq!(
        failed.entries[0].branch.as_ref().unwrap().remote.status,
        CleanupBranchActionStatus::Deleted
    );
    failed.entries[0].status = CleanupReceiptStatus::Failed;
    failed.entries[0].reason = Some("downstream cleanup failed after remote deletion".to_string());
    service.persist_receipt(&failed).await.unwrap();

    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Cleaned);
    assert!(receipt.entries[0]
        .actions
        .contains(&"delete_remote_branch".to_string()));
    assert!(!fixture.worktree.exists());
    assert!(!fixture.agent_dir.exists());
    assert!(fixture
        .services
        .agent_resolver
        .get(&fixture.record.agent_name)
        .await
        .is_none());
}

#[tokio::test]
async fn recreated_remote_branch_invalidates_prior_deletion_proof() {
    let fixture = real_cleanup_fixture().await;
    let service = fixture.services.cleanup_service();
    let request = CleanupRequest {
        target: Some(fixture.record.agent_name.to_string()),
        sweep: false,
        apply: true,
        delete_remote_branch: true,
        allow_no_pr: true,
        discard_dirty: true,
        ..CleanupRequest::default()
    };

    service.fail_receipt_persist_on_call(6);
    assert!(service.run(&request).await.is_err());
    service.fail_receipt_persist_on_call(0);
    let plan = service.plan(&request).await.unwrap();
    let mut failed = service
        .resume_or_create_receipt(&plan, request.target.as_deref(), 1)
        .await
        .unwrap();
    failed.entries[0].status = CleanupReceiptStatus::Failed;
    failed.entries[0].reason = Some("downstream cleanup failed after remote deletion".to_string());
    service.persist_receipt(&failed).await.unwrap();
    run_git(
        fixture.services.project_dir.as_path(),
        &["push", "-q", "origin", "main.stale"],
    );

    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Refused);
    assert!(receipt.entries[0].reason.as_deref().is_some_and(|reason| {
        reason.contains("recreated or moved") || reason.contains("remote branch")
    }));
    assert!(fixture.agent_dir.exists());
    assert!(remote_branch_state(
        fixture.services.project_dir.as_path(),
        "origin",
        "main.stale"
    )
    .await
    .unwrap()
    .is_some());
}

#[tokio::test]
async fn no_pr_remote_cleanup_uses_verified_remote_head_and_preserves_unique_commits() {
    let fixture = real_cleanup_fixture().await;
    let service = fixture.services.cleanup_service();
    let preserved_head = local_branch_state(fixture.services.project_dir.as_path(), "main.stale")
        .await
        .unwrap()
        .unwrap();
    let request = CleanupRequest {
        target: Some(fixture.record.agent_name.to_string()),
        sweep: false,
        apply: true,
        delete_remote_branch: true,
        reason: Some("preserved unique abandoned commits".to_string()),
        allow_no_pr: true,
        discard_dirty: true,
        preserve_unique_commits: true,
    };

    let plan = service.plan(&request).await.unwrap();
    let candidate = plan
        .candidates
        .iter()
        .find(|candidate| candidate.id == fixture.record.agent_name.as_str())
        .unwrap();
    assert_eq!(candidate.decision, CleanupDecision::Cleanable);
    assert_eq!(
        candidate.branch.as_ref().unwrap().local.status,
        CleanupBranchActionStatus::Skipped
    );
    assert_eq!(
        candidate.branch.as_ref().unwrap().remote.status,
        CleanupBranchActionStatus::WouldDelete
    );

    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Cleaned);
    assert!(receipt.entries[0]
        .actions
        .contains(&"preserve_unique_commits".to_string()));
    assert!(receipt.entries[0]
        .actions
        .contains(&"delete_remote_branch_override".to_string()));
    assert!(!fixture.worktree.exists());
    assert!(!fixture.agent_dir.exists());
    assert_eq!(
        local_branch_state(fixture.services.project_dir.as_path(), "main.stale")
            .await
            .unwrap(),
        Some(preserved_head)
    );
    assert!(remote_branch_state(
        fixture.services.project_dir.as_path(),
        "origin",
        "main.stale"
    )
    .await
    .unwrap()
    .is_none());
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
async fn remote_deletion_refuses_an_absent_branch_without_a_prior_receipt() {
    let fixture = real_cleanup_fixture().await;
    let service = fixture.services.cleanup_service();
    run_git(
        fixture.services.project_dir.as_path(),
        &["push", "-q", "origin", "--delete", "main.stale"],
    );
    let request = CleanupRequest {
        target: Some(fixture.record.agent_name.to_string()),
        sweep: false,
        apply: true,
        delete_remote_branch: true,
        allow_no_pr: true,
        discard_dirty: true,
        ..CleanupRequest::default()
    };

    let receipt = service.run(&request).await.unwrap();
    assert_eq!(receipt.entries[0].status, CleanupReceiptStatus::Refused);
    assert!(receipt.entries[0]
        .reason
        .as_deref()
        .is_some_and(|reason| reason.contains("remote deletion")));
    assert_eq!(
        receipt.entries[0].branch.as_ref().unwrap().remote.status,
        CleanupBranchActionStatus::Refused
    );
    assert!(fixture.worktree.exists());
    assert!(fixture.agent_dir.exists());
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
        recovered_provenance: None,
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
        preserve_unique_commits: false,
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
        operator_reason: None,
        preserve_unique_commits: false,
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
