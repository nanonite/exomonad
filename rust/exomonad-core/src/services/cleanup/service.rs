use super::support::*;
use super::types::*;
use crate::domain::BranchName;
use crate::services::agent_control::{read_invocation, Topology};
use crate::services::agent_resolver::{AgentIdentityRecord, AgentResolver};
use crate::services::forgejo::ForgejoClient;
use crate::services::git_worktree::GitWorktreeService;
use crate::services::mutex_registry::MutexRegistry;
use crate::services::repo::{get_repository_identity, RepositoryIdentity};
use crate::services::tmux_ipc::{routing_target_alive, TmuxIpc};
use crate::services::Services;
use anyhow::{bail, Context, Result};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;
use tokio::fs;
use uuid::Uuid;

const LOCK_TIMEOUT: Duration = Duration::from_secs(30);
const LOCK_ESTIMATE: Duration = Duration::from_secs(120);

#[derive(Clone)]
pub struct VerifiedCleanupService {
    project_dir: PathBuf,
    resolver: Arc<AgentResolver>,
    git_worktree: Arc<GitWorktreeService>,
    forgejo: Option<Arc<ForgejoClient>>,
    mutex: Arc<MutexRegistry>,
}

impl VerifiedCleanupService {
    pub fn new(
        project_dir: impl Into<PathBuf>,
        resolver: Arc<AgentResolver>,
        git_worktree: Arc<GitWorktreeService>,
        forgejo: Option<Arc<ForgejoClient>>,
        mutex: Arc<MutexRegistry>,
    ) -> Self {
        let requested_dir = project_dir.into();
        let project_dir = std::fs::canonicalize(&requested_dir).unwrap_or(requested_dir);
        Self {
            project_dir,
            resolver,
            git_worktree,
            forgejo,
            mutex,
        }
    }

    pub fn from_services(services: &Services) -> Self {
        Self::new(
            services.project_dir.clone(),
            services.agent_resolver.clone(),
            services.git_wt.clone(),
            services.forgejo_client.clone(),
            services.mutex_registry.clone(),
        )
    }

    pub fn receipt_dir(&self) -> PathBuf {
        self.project_dir.join(".exo/cleanup/receipts")
    }

    pub async fn plan(&self, request: &CleanupRequest) -> Result<CleanupPlan> {
        request.validate()?;
        let records = self.discover_records(request).await?;
        let requires_remote = records
            .iter()
            .any(|(_, record)| record.topology == Topology::WorktreePerAgent);
        let (repository, repository_error) = if requires_remote {
            match get_repository_identity(&self.project_dir).await {
                Ok(identity) => (Some(identity), None),
                Err(error) => (None, Some(error.to_string())),
            }
        } else {
            (None, None)
        };
        let current_branch = current_branch(&self.project_dir).await;
        let mut candidates = Vec::with_capacity(records.len());
        for (entry_name, record) in records {
            candidates.push(
                self.inspect_candidate(
                    entry_name,
                    record,
                    repository.as_ref(),
                    repository_error.as_deref(),
                    current_branch.as_deref(),
                )
                .await,
            );
        }
        refuse_duplicate_branches(&mut candidates);
        Ok(CleanupPlan {
            schema_version: CLEANUP_PLAN_SCHEMA_VERSION,
            plan_id: Uuid::new_v4().to_string(),
            generated_at: unix_timestamp(),
            project_dir: self.project_dir.clone(),
            repository,
            repository_error,
            candidates,
        })
    }

    pub async fn run(&self, request: &CleanupRequest) -> Result<CleanupReceipt> {
        if request.apply {
            return self.apply(request).await;
        }
        let plan = self.plan(request).await?;
        let receipt = dry_run_receipt(&plan);
        self.persist_receipt(&receipt).await?;
        Ok(receipt)
    }

    pub async fn apply(&self, request: &CleanupRequest) -> Result<CleanupReceipt> {
        request.validate()?;
        let resource = format!("cleanup:{}", self.project_dir.display());
        let lock = self
            .mutex
            .acquire(
                resource.clone(),
                "cleanup".to_string(),
                "verified cleanup apply".to_string(),
                LOCK_ESTIMATE,
                LOCK_TIMEOUT,
            )
            .await;
        if !lock.acquired {
            bail!(
                "cleanup is already in progress: {}",
                if lock.holder_intent.is_empty() {
                    "lock unavailable".to_string()
                } else {
                    lock.holder_intent
                }
            );
        }

        let started_at = unix_timestamp();
        let result = async {
            let plan = self.plan(request).await?;
            self.execute_plan(&plan, started_at).await
        }
        .await;
        let released = self.mutex.release(resource, lock.lock_id).await;
        if !released {
            tracing::warn!("cleanup lock was not released by its owner");
        }
        let receipt = result?;
        self.persist_receipt(&receipt).await?;
        Ok(receipt)
    }

    async fn discover_records(
        &self,
        request: &CleanupRequest,
    ) -> Result<Vec<(String, AgentIdentityRecord)>> {
        let agents_dir = self.project_dir.join(".exo/agents");
        let mut entries = match fs::read_dir(&agents_dir).await {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Vec::new());
            }
            Err(error) => {
                return Err(error).with_context(|| format!("read {}", agents_dir.display()));
            }
        };
        let mut records = Vec::new();
        while let Some(entry) = entries.next_entry().await? {
            let file_type = entry.file_type().await?;
            if !file_type.is_dir() || file_type.is_symlink() {
                continue;
            }
            let entry_name = match entry.file_name().to_str() {
                Some(name) if !name.is_empty() => name.to_string(),
                _ => continue,
            };
            let identity_path = entry.path().join("identity.json");
            let Ok(contents) = fs::read_to_string(&identity_path).await else {
                continue;
            };
            let Ok(identity) = serde_json::from_str::<AgentIdentityRecord>(&contents) else {
                continue;
            };
            if requested_target_matches(request.target.as_deref(), &entry_name, &identity) {
                records.push((entry_name, identity));
            }
        }
        if let Some(target) = &request.target {
            if records.is_empty() {
                bail!("managed cleanup target {:?} was not found", target);
            }
        }
        records.sort_by(|left, right| left.0.cmp(&right.0));
        Ok(records)
    }

    async fn inspect_candidate(
        &self,
        entry_name: String,
        identity: AgentIdentityRecord,
        repository: Option<&RepositoryIdentity>,
        repository_error: Option<&str>,
        current_branch: Option<&str>,
    ) -> CleanupCandidate {
        let agent_dir = self.project_dir.join(".exo/agents").join(&entry_name);
        let resolved_working_dir = resolve_path(&self.project_dir, &identity.working_dir);
        let mut identity_drift = entry_name != identity.agent_name.as_str()
            || !path_within(&self.project_dir, &resolved_working_dir);
        let worktree_path = if identity.topology == Topology::WorktreePerAgent {
            let worktree_root = self.project_dir.join(".exo/worktrees");
            if !path_within(&worktree_root, &resolved_working_dir) {
                identity_drift = true;
            }
            Some(resolved_working_dir.clone())
        } else {
            None
        };

        let mut local_branch = None;
        let mut local_head_sha = None;
        let mut dirty = Some(false);
        if let Some(worktree) = &worktree_path {
            if worktree.exists() {
                match fs::canonicalize(worktree).await {
                    Ok(canonical_worktree) => {
                        if !path_within(&self.project_dir, &canonical_worktree)
                            || !path_within(
                                &self.project_dir.join(".exo/worktrees"),
                                &canonical_worktree,
                            )
                        {
                            identity_drift = true;
                        }
                        match workspace_git_root(worktree).await {
                            Ok(Some(root)) if root != canonical_worktree => identity_drift = true,
                            Ok(Some(_)) => {}
                            Ok(None) => identity_drift = true,
                            Err(_) => identity_drift = true,
                        }
                        match workspace_branch(worktree).await {
                            Ok(Some(actual)) if actual != identity.birth_branch.as_str() => {
                                identity_drift = true
                            }
                            Ok(_) => {}
                            Err(_) => identity_drift = true,
                        }
                        dirty = workspace_dirty(worktree).await.ok();
                    }
                    Err(_) => identity_drift = true,
                }
            }
        }

        if let Ok(Some(sha)) =
            local_branch_state(&self.project_dir, identity.birth_branch.as_str()).await
        {
            local_branch = Some(identity.birth_branch.to_string());
            local_head_sha = Some(sha);
        }

        let (remote_branch, remote_head_sha, remote_error) = match repository {
            Some(repository) => match remote_branch_state(
                &self.project_dir,
                &repository.remote_name,
                identity.birth_branch.as_str(),
            )
            .await
            {
                Ok(Some(sha)) => (Some(identity.birth_branch.to_string()), Some(sha), None),
                Ok(None) => (None, None, None),
                Err(error) => (None, None, Some(error.to_string())),
            },
            None => (None, None, None),
        };

        let (pull_request, pr_error) = self
            .pull_request_state(&identity, repository, repository_error)
            .await;
        let head_matches_pull_request = match (&local_head_sha, &pull_request) {
            (Some(local), Some(pr)) if pr.head_sha.is_some() => {
                Some(pr.head_sha.as_ref() == Some(local))
            }
            _ => None,
        };
        let protected = identity.birth_branch.as_str() == current_branch.unwrap_or("")
            || repository.is_some_and(|repo| identity.birth_branch.as_str() == repo.base_branch)
            || matches!(
                identity.birth_branch.as_str(),
                "main" | "master" | "trunk" | "develop"
            );
        let liveness = self.liveness(&agent_dir).await;
        let decision = candidate_decision(DecisionContext {
            identity: &identity,
            liveness: &liveness,
            dirty,
            protected,
            identity_drift,
            repository,
            repository_error,
            remote_error: remote_error.as_deref(),
            pull_request: pull_request.as_ref(),
            pr_error: pr_error.as_deref(),
            head_matches_pull_request,
        });
        CleanupCandidate {
            id: entry_name,
            managed: true,
            agent_name: identity.agent_name.to_string(),
            issue: identity.slice_id.clone(),
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
            identity_drift,
            head_matches_pull_request,
            identity,
            decision,
        }
    }

    async fn pull_request_state(
        &self,
        identity: &AgentIdentityRecord,
        repository: Option<&RepositoryIdentity>,
        repository_error: Option<&str>,
    ) -> (Option<CleanupPullRequest>, Option<String>) {
        if identity.topology != Topology::WorktreePerAgent {
            return (None, None);
        }
        let Some(repository) = repository else {
            return (None, repository_error.map(ToOwned::to_owned));
        };
        let Some(forgejo) = &self.forgejo else {
            return (None, Some("Forgejo client is not configured".to_string()));
        };
        let branch = match BranchName::try_from_str(identity.birth_branch.as_str()) {
            Ok(branch) => branch,
            Err(error) => return (None, Some(error.to_string())),
        };
        match forgejo
            .find_pull_requests_by_head(&repository.owner, &repository.repo, &branch)
            .await
        {
            Ok(prs) if prs.len() == 1 => (Some(prs[0].clone().into()), None),
            Ok(prs) if prs.is_empty() => {
                (None, Some("no pull request owns this branch".to_string()))
            }
            Ok(prs) => (
                None,
                Some(format!("{} pull requests own this branch", prs.len())),
            ),
            Err(error) => (None, Some(error.to_string())),
        }
    }

    async fn liveness(&self, agent_dir: &Path) -> CleanupLiveness {
        if !agent_dir.is_dir() {
            return CleanupLiveness::Unknown;
        }
        let marked_terminal =
            agent_dir.join("exited_at").exists() || agent_dir.join("exit_code").exists();
        let invocation_terminal = match read_invocation(agent_dir).await {
            Ok(Some(invocation)) if invocation.is_live() => return CleanupLiveness::Live,
            Ok(Some(_)) => true,
            Ok(None) => false,
            Err(_) => return CleanupLiveness::Unknown,
        };
        let routing_path = agent_dir.join("routing.json");
        if !routing_path.exists() {
            return if marked_terminal || invocation_terminal {
                CleanupLiveness::Dead
            } else {
                CleanupLiveness::Unknown
            };
        }
        let Ok(routing) = crate::domain::RoutingInfo::read_from_dir(agent_dir).await else {
            return CleanupLiveness::Unknown;
        };
        if !routing.has_delivery_target() {
            return CleanupLiveness::Dead;
        }
        let Ok(session) = std::env::var("EXOMONAD_TMUX_SESSION") else {
            return CleanupLiveness::Unknown;
        };
        if session.trim().is_empty() {
            return CleanupLiveness::Unknown;
        }
        let tmux = TmuxIpc::new(&session);
        let Ok(true) = routing_target_alive(&routing, &tmux).await else {
            return CleanupLiveness::Unknown;
        };
        match tmux.routing_target_process_alive(&routing).await {
            Ok(true) => CleanupLiveness::Live,
            Ok(false) if marked_terminal || invocation_terminal => CleanupLiveness::Dead,
            Ok(false) => CleanupLiveness::Dead,
            Err(_) => CleanupLiveness::Unknown,
        }
    }

    async fn execute_plan(&self, plan: &CleanupPlan, started_at: u64) -> Result<CleanupReceipt> {
        let mut entries = Vec::with_capacity(plan.candidates.len());
        for candidate in &plan.candidates {
            if !candidate.decision.is_cleanable() {
                entries.push(receipt_entry(
                    candidate,
                    CleanupReceiptStatus::Refused,
                    Vec::new(),
                    candidate.decision.reason().map(ToOwned::to_owned),
                ));
                continue;
            }
            entries.push(self.execute_candidate(candidate).await);
        }
        Ok(CleanupReceipt {
            schema_version: CLEANUP_RECEIPT_SCHEMA_VERSION,
            operation_id: Uuid::new_v4().to_string(),
            plan_id: plan.plan_id.clone(),
            started_at,
            finished_at: unix_timestamp(),
            dry_run: false,
            entries,
        })
    }

    async fn execute_candidate(&self, candidate: &CleanupCandidate) -> CleanupReceiptEntry {
        let Ok(agent_metadata) = fs::symlink_metadata(&candidate.agent_dir).await else {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Skipped,
                vec!["already_absent".to_string()],
                Some("agent directory is already absent".to_string()),
            );
        };
        if !agent_metadata.file_type().is_dir() || agent_metadata.file_type().is_symlink() {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Refused,
                Vec::new(),
                Some("agent directory is not a real managed directory".to_string()),
            );
        }
        let identity_path = candidate.agent_dir.join("identity.json");
        let current_identity = match fs::read_to_string(&identity_path).await {
            Ok(contents) => match serde_json::from_str::<AgentIdentityRecord>(&contents) {
                Ok(identity) => identity,
                Err(_) => {
                    return receipt_entry(
                        candidate,
                        CleanupReceiptStatus::Refused,
                        Vec::new(),
                        Some("identity changed or is malformed".to_string()),
                    )
                }
            },
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return receipt_entry(
                    candidate,
                    CleanupReceiptStatus::Skipped,
                    vec!["already_absent".to_string()],
                    Some("agent identity is already absent".to_string()),
                )
            }
            Err(error) => {
                return receipt_entry(
                    candidate,
                    CleanupReceiptStatus::Failed,
                    Vec::new(),
                    Some(format!("read identity: {error}")),
                )
            }
        };
        if current_identity != candidate.identity {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Refused,
                Vec::new(),
                Some("identity changed since planning".to_string()),
            );
        }
        if self.liveness(&candidate.agent_dir).await != CleanupLiveness::Dead {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Refused,
                Vec::new(),
                Some("agent is no longer provably dead".to_string()),
            );
        }
        if let Some(worktree) = &candidate.worktree_path {
            if worktree.exists() {
                match workspace_dirty(worktree).await {
                    Ok(false) => {}
                    _ => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Refused,
                            Vec::new(),
                            Some("worktree is dirty or unavailable".to_string()),
                        )
                    }
                }
                if let Ok(Some(actual_branch)) = workspace_branch(worktree).await {
                    if actual_branch != candidate.identity.birth_branch.as_str() {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Refused,
                            Vec::new(),
                            Some("worktree branch changed since planning".to_string()),
                        );
                    }
                }
                let Ok(canonical) = fs::canonicalize(worktree).await else {
                    return receipt_entry(
                        candidate,
                        CleanupReceiptStatus::Refused,
                        Vec::new(),
                        Some("worktree path changed since planning".to_string()),
                    );
                };
                if !path_within(&self.project_dir.join(".exo/worktrees"), &canonical) {
                    return receipt_entry(
                        candidate,
                        CleanupReceiptStatus::Refused,
                        Vec::new(),
                        Some("worktree path is outside the managed root".to_string()),
                    );
                }
            }
        }

        let mut actions = Vec::new();
        if let Some(worktree) = &candidate.worktree_path {
            if worktree.exists() {
                let git_worktree = self.git_worktree.clone();
                let path = worktree.clone();
                match tokio::task::spawn_blocking(move || git_worktree.remove_workspace(&path))
                    .await
                {
                    Ok(Ok(())) => actions.push("remove_worktree".to_string()),
                    Ok(Err(error)) => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Failed,
                            actions,
                            Some(format!("remove worktree: {error}")),
                        )
                    }
                    Err(error) => {
                        return receipt_entry(
                            candidate,
                            CleanupReceiptStatus::Failed,
                            actions,
                            Some(format!("remove worktree task: {error}")),
                        )
                    }
                }
            } else {
                actions.push("worktree_already_absent".to_string());
            }
        }
        match fs::remove_dir_all(&candidate.agent_dir).await {
            Ok(()) => actions.push("remove_agent_directory".to_string()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                actions.push("agent_directory_already_absent".to_string())
            }
            Err(error) => {
                return receipt_entry(
                    candidate,
                    CleanupReceiptStatus::Failed,
                    actions,
                    Some(format!("remove agent directory: {error}")),
                )
            }
        }
        if let Ok(agent_name) = crate::domain::AgentName::try_from_str(&candidate.agent_name) {
            if let Err(error) = self.resolver.deregister(&agent_name).await {
                return receipt_entry(
                    candidate,
                    CleanupReceiptStatus::Failed,
                    actions,
                    Some(format!("deregister identity: {error}")),
                );
            }
            actions.push("deregister_identity".to_string());
        } else {
            return receipt_entry(
                candidate,
                CleanupReceiptStatus::Failed,
                actions,
                Some("agent name became invalid".to_string()),
            );
        }
        receipt_entry(candidate, CleanupReceiptStatus::Cleaned, actions, None)
    }

    async fn persist_receipt(&self, receipt: &CleanupReceipt) -> Result<()> {
        let dir = self.receipt_dir();
        fs::create_dir_all(&dir).await?;
        let path = dir.join(format!("{}.json", receipt.operation_id));
        let temporary = dir.join(format!(".{}.tmp", receipt.operation_id));
        let bytes = serde_json::to_vec_pretty(receipt)?;
        fs::write(&temporary, bytes).await?;
        fs::rename(&temporary, &path).await?;
        Ok(())
    }
}
