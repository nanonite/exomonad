use super::support::*;
use super::types::*;
use crate::services::agent_control::Topology;
use crate::services::agent_resolver::AgentResolver;
use crate::services::forgejo::ForgejoClient;
use crate::services::git_worktree::GitWorktreeService;
use crate::services::mutex_registry::MutexRegistry;
use crate::services::repo::get_repository_identity;
use crate::services::Services;
use anyhow::{bail, Result};
use std::path::PathBuf;
#[cfg(test)]
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;
use uuid::Uuid;

const LOCK_TIMEOUT: Duration = Duration::from_secs(30);
const LOCK_ESTIMATE: Duration = Duration::from_secs(120);

#[derive(Clone)]
pub struct VerifiedCleanupService {
    pub(super) project_dir: PathBuf,
    pub(super) resolver: Arc<AgentResolver>,
    pub(super) git_worktree: Arc<GitWorktreeService>,
    pub(super) forgejo: Option<Arc<ForgejoClient>>,
    pub(super) mutex: Arc<MutexRegistry>,
    #[cfg(test)]
    pub(super) receipt_persist_calls: Arc<AtomicUsize>,
    #[cfg(test)]
    pub(super) fail_receipt_persist_on: Arc<AtomicUsize>,
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
            #[cfg(test)]
            receipt_persist_calls: Arc::new(AtomicUsize::new(0)),
            #[cfg(test)]
            fail_receipt_persist_on: Arc::new(AtomicUsize::new(0)),
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

    #[cfg(test)]
    pub(super) fn fail_receipt_persist_on_call(&self, call: usize) {
        self.fail_receipt_persist_on.store(call, Ordering::SeqCst);
    }

    pub async fn plan(&self, request: &CleanupRequest) -> Result<CleanupPlan> {
        request.validate()?;
        let resources = self.discover_resources(request).await?;
        let requires_remote = resources.iter().any(|resource| {
            !resource.resolver_only
                && (resource.worktree_path.is_some()
                    || resource
                        .identity
                        .as_ref()
                        .is_some_and(|identity| identity.topology == Topology::WorktreePerAgent))
        });
        let (repository, repository_error) = if requires_remote {
            match get_repository_identity(&self.project_dir).await {
                Ok(identity) => (Some(identity), None),
                Err(error) => (None, Some(error.to_string())),
            }
        } else {
            (None, None)
        };
        let current_branch = current_branch(&self.project_dir).await;
        let mut candidates = Vec::with_capacity(resources.len());
        for resource in resources {
            candidates.push(
                self.inspect_candidate(
                    resource,
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
            let mut receipt = self
                .resume_or_create_receipt(&plan, request.target.as_deref(), started_at)
                .await?;
            self.persist_receipt(&receipt).await?;
            self.execute_plan(&plan, &mut receipt).await?;
            receipt.finished_at = unix_timestamp();
            self.persist_receipt(&receipt).await?;
            Ok(receipt)
        }
        .await;
        let released = self.mutex.release(resource, lock.lock_id).await;
        if !released {
            tracing::warn!("cleanup lock was not released by its owner");
        }
        result
    }
}
