use super::discovery::DiscoveredResource;
use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::domain::BranchName;
use crate::services::agent_control::{read_invocation, Topology};
use crate::services::repo::RepositoryIdentity;
use crate::services::tmux_ipc::{routing_target_alive, TmuxIpc};
use std::path::Path;
use tokio::fs;

impl VerifiedCleanupService {
    pub(super) async fn inspect_candidate(
        &self,
        resource: DiscoveredResource,
        repository: Option<&RepositoryIdentity>,
        repository_error: Option<&str>,
        current_branch: Option<&str>,
    ) -> CleanupCandidate {
        let DiscoveredResource {
            id,
            agent_dir,
            worktree_path: discovered_worktree,
            identity,
            mut identity_error,
        } = resource;
        let resolved_working_dir = identity
            .as_ref()
            .map(|identity| resolve_path(&self.project_dir, &identity.working_dir))
            .or_else(|| discovered_worktree.clone());
        let mut identity_drift = identity_error.is_some() || identity.is_none();
        if identity
            .as_ref()
            .is_some_and(|identity| id != identity.agent_name.as_str())
            || resolved_working_dir
                .as_ref()
                .is_some_and(|path| !path_within(&self.project_dir, path))
        {
            identity_drift = true;
        }
        let worktree_path = if identity
            .as_ref()
            .is_some_and(|identity| identity.topology == Topology::WorktreePerAgent)
            || discovered_worktree.is_some()
        {
            let worktree = discovered_worktree.or_else(|| resolved_working_dir.clone());
            if let Some(worktree) = &worktree {
                let worktree_root = self.project_dir.join(".exo/worktrees");
                if !path_within(&worktree_root, worktree) {
                    identity_drift = true;
                }
            }
            worktree
        } else {
            None
        };

        let observed_worktree_branch = if let Some(worktree) = &worktree_path {
            workspace_branch(worktree).await.ok().flatten()
        } else {
            None
        };
        let branch_name = worktree_path.as_ref().and_then(|_| {
            identity
                .as_ref()
                .map(|identity| identity.birth_branch.as_str())
                .or(observed_worktree_branch.as_deref())
        });
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
                            Ok(Some(actual)) => {
                                if identity.as_ref().is_some_and(|identity| {
                                    actual != identity.birth_branch.as_str()
                                }) {
                                    identity_drift = true;
                                }
                            }
                            Ok(None) | Err(_) => identity_drift = true,
                        }
                        dirty = workspace_dirty(worktree).await.ok();
                    }
                    Err(_) => identity_drift = true,
                }
            }
        }

        if let Some(branch) = branch_name {
            match local_branch_state(&self.project_dir, branch).await {
                Ok(Some(sha)) => {
                    local_branch = Some(branch.to_string());
                    local_head_sha = Some(sha);
                }
                Ok(None) => {
                    identity_error = Some("managed local branch is unavailable".to_string());
                }
                Err(error) => {
                    identity_error = Some(format!("read local branch: {error}"));
                }
            }
        }

        let (remote_branch, remote_head_sha, remote_error) =
            if let (Some(repository), Some(branch)) = (repository, branch_name) {
                match remote_branch_state(&self.project_dir, &repository.remote_name, branch).await
                {
                    Ok(Some(sha)) => (Some(branch.to_string()), Some(sha), None),
                    Ok(None) => (None, None, None),
                    Err(error) => (None, None, Some(error.to_string())),
                }
            } else {
                (None, None, None)
            };

        let (pull_request, pr_error) = self
            .pull_request_state(
                branch_name,
                worktree_path.is_some(),
                repository,
                repository_error,
            )
            .await;
        let head_matches_pull_request = match (&local_head_sha, &pull_request) {
            (Some(local), Some(pr)) => Some(pr.head_sha.as_ref() == Some(local)),
            _ => None,
        };
        let remote_head_matches_pull_request = match (&remote_head_sha, &pull_request) {
            (Some(remote), Some(pr)) => Some(pr.head_sha.as_ref() == Some(remote)),
            _ => None,
        };
        let protected = branch_name.is_some_and(|branch| {
            branch == current_branch.unwrap_or("")
                || repository.is_some_and(|repo| branch == repo.base_branch)
                || matches!(branch, "main" | "master" | "trunk" | "develop")
        });
        let liveness = self.liveness(&agent_dir).await;
        let decision = candidate_decision(DecisionContext {
            identity: identity.as_ref(),
            identity_error: identity_error.as_deref(),
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
            remote_head_matches_pull_request,
        });
        let agent_name = identity
            .as_ref()
            .map(|identity| identity.agent_name.to_string())
            .unwrap_or_else(|| id.clone());
        CleanupCandidate {
            id,
            managed: true,
            agent_name,
            issue: read_active_issue(&agent_dir).await,
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
            identity_error,
            head_matches_pull_request,
            remote_head_matches_pull_request,
            identity,
            decision,
        }
    }

    async fn pull_request_state(
        &self,
        branch_name: Option<&str>,
        is_worktree: bool,
        repository: Option<&RepositoryIdentity>,
        repository_error: Option<&str>,
    ) -> (Option<CleanupPullRequest>, Option<String>) {
        if !is_worktree {
            return (None, None);
        }
        let Some(branch_name) = branch_name else {
            return (
                None,
                Some("managed worktree branch is unavailable".to_string()),
            );
        };
        let Some(repository) = repository else {
            return (None, repository_error.map(ToOwned::to_owned));
        };
        let Some(forgejo) = &self.forgejo else {
            return (None, Some("Forgejo client is not configured".to_string()));
        };
        let branch = match BranchName::try_from_str(branch_name) {
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

    pub(super) async fn liveness(&self, agent_dir: &Path) -> CleanupLiveness {
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
        match classify_routing_target(routing_target_alive(&routing, &tmux).await) {
            CleanupLiveness::Dead => return CleanupLiveness::Dead,
            CleanupLiveness::Unknown => return CleanupLiveness::Unknown,
            CleanupLiveness::Live => {}
        }
        match tmux.routing_target_process_alive(&routing).await {
            Ok(true) => CleanupLiveness::Live,
            Ok(false) if marked_terminal || invocation_terminal => CleanupLiveness::Dead,
            Ok(false) => CleanupLiveness::Dead,
            Err(_) => CleanupLiveness::Unknown,
        }
    }
}
