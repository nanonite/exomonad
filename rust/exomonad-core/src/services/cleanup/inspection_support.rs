use super::service::VerifiedCleanupService;
use super::support::*;
use super::types::*;
use crate::domain::BranchName;
use crate::services::agent_control::read_invocation;
use crate::services::repo::RepositoryIdentity;
use crate::services::tmux_ipc::{routing_target_alive, TmuxIpc};
use std::path::Path;

impl VerifiedCleanupService {
    pub(super) async fn pull_request_state(
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
        if !agent_dir.join("routing.json").exists() {
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
        let Some(session) = self.tmux_session.as_deref() else {
            return CleanupLiveness::Unknown;
        };
        if session.trim().is_empty() {
            return CleanupLiveness::Unknown;
        }
        let tmux = TmuxIpc::new(session);
        match classify_routing_target(routing_target_alive(&routing, &tmux).await) {
            CleanupLiveness::Dead => return CleanupLiveness::Dead,
            CleanupLiveness::Unknown => return CleanupLiveness::Unknown,
            CleanupLiveness::Live => {}
        }
        match tmux.routing_target_process_alive(&routing).await {
            Ok(true) => CleanupLiveness::Live,
            Ok(false) => CleanupLiveness::Dead,
            Err(_) => CleanupLiveness::Unknown,
        }
    }
}
