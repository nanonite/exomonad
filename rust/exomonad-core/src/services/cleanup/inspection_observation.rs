use super::service::VerifiedCleanupService;
use super::support::*;
use crate::services::agent_control::Topology;
use crate::services::agent_resolver::AgentIdentityRecord;
use std::path::PathBuf;
use tokio::fs;

pub(super) struct WorktreeObservation {
    pub(super) worktree_path: Option<PathBuf>,
    pub(super) branch_name: Option<String>,
    pub(super) dirty: Option<bool>,
    pub(super) identity_drift: bool,
}

impl VerifiedCleanupService {
    pub(super) async fn observe_worktree(
        &self,
        id: &str,
        discovered_worktree: Option<PathBuf>,
        identity: Option<&AgentIdentityRecord>,
        identity_error: Option<&str>,
    ) -> WorktreeObservation {
        let resolved_working_dir = identity
            .map(|identity| resolve_path(&self.project_dir, &identity.working_dir))
            .or_else(|| discovered_worktree.clone());
        let mut identity_drift = identity_error.is_some() || identity.is_none();
        identity_drift |= identity.is_some_and(|identity| id != identity.agent_name.as_str());
        identity_drift |= resolved_working_dir
            .as_ref()
            .is_some_and(|path| !path_within(&self.project_dir, path));
        let worktree_path = self.derive_worktree_path(
            discovered_worktree,
            resolved_working_dir,
            identity,
            &mut identity_drift,
        );
        let observed_branch = if let Some(worktree) = &worktree_path {
            workspace_branch(worktree).await.ok().flatten()
        } else {
            None
        };
        let branch_name = worktree_path.as_ref().and_then(|_| {
            identity
                .map(|identity| identity.birth_branch.to_string())
                .or(observed_branch)
        });
        let (dirty, worktree_drift) = self
            .observe_existing_worktree(
                &worktree_path,
                identity.map(|identity| identity.birth_branch.as_str()),
            )
            .await;
        identity_drift |= worktree_drift;
        WorktreeObservation {
            worktree_path,
            branch_name,
            dirty,
            identity_drift,
        }
    }

    fn derive_worktree_path(
        &self,
        discovered_worktree: Option<PathBuf>,
        resolved_working_dir: Option<PathBuf>,
        identity: Option<&AgentIdentityRecord>,
        identity_drift: &mut bool,
    ) -> Option<PathBuf> {
        if !identity.is_some_and(|identity| identity.topology == Topology::WorktreePerAgent)
            && discovered_worktree.is_none()
        {
            return None;
        }
        let worktree = discovered_worktree.or(resolved_working_dir);
        if worktree
            .as_ref()
            .is_some_and(|path| !path_within(&self.project_dir.join(".exo/worktrees"), path))
        {
            *identity_drift = true;
        }
        worktree
    }

    async fn observe_existing_worktree(
        &self,
        worktree_path: &Option<PathBuf>,
        expected_branch: Option<&str>,
    ) -> (Option<bool>, bool) {
        let Some(worktree) = worktree_path else {
            return (Some(false), false);
        };
        if !worktree.exists() {
            return (Some(false), false);
        }
        let Ok(canonical_worktree) = fs::canonicalize(worktree).await else {
            return (None, true);
        };
        let mut drift = !path_within(&self.project_dir, &canonical_worktree)
            || !path_within(
                &self.project_dir.join(".exo/worktrees"),
                &canonical_worktree,
            );
        drift |= match workspace_git_root(worktree).await {
            Ok(Some(root)) => root != canonical_worktree,
            Ok(None) | Err(_) => true,
        };
        drift |= match workspace_branch(worktree).await {
            Ok(Some(branch)) => expected_branch.is_some_and(|expected| branch != expected),
            Ok(None) | Err(_) => true,
        };
        (workspace_dirty(worktree).await.ok(), drift)
    }
}
