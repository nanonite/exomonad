use crate::domain::RoutingInfo;
use crate::services::git_worktree::GitWorktreeService;
use crate::services::tmux_ipc::TmuxIpc;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tracing::{debug, info, warn};

pub async fn dispose_reviewers_for_pr(
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
    pr_number: u64,
) -> Vec<String> {
    let slugs = reviewer_slugs_for_pr(project_dir, pr_number).await;
    for slug in &slugs {
        info!(pr_number, reviewer = %slug, "Disposing ephemeral reviewer agent");
        dispose_agent_resources(project_dir, git_wt.clone(), slug).await;
    }
    slugs
}

async fn reviewer_slugs_for_pr(project_dir: &Path, pr_number: u64) -> Vec<String> {
    let worktrees_dir = project_dir.join(".exo/worktrees");
    let Ok(mut entries) = tokio::fs::read_dir(&worktrees_dir).await else {
        return Vec::new();
    };

    let mut slugs = Vec::new();
    while let Ok(Some(entry)) = entries.next_entry().await {
        let Ok(file_type) = entry.file_type().await else {
            continue;
        };
        if !file_type.is_dir() {
            continue;
        }
        let Some(name) = entry.file_name().to_str().map(str::to_string) else {
            continue;
        };
        if is_reviewer_slug_for_pr(&name, pr_number) {
            slugs.push(name);
        }
    }
    slugs.sort();
    slugs
}

fn is_reviewer_slug_for_pr(slug: &str, pr_number: u64) -> bool {
    let prefix = format!("review-pr-{}-", pr_number);
    slug.starts_with(&prefix)
}

pub async fn dispose_agent_resources(
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
    agent_slug: &str,
) {
    let worktree_path = project_dir.join(".exo/worktrees").join(agent_slug);
    close_agent_tmux_window(project_dir, agent_slug, &worktree_path).await;
    cleanup_worker_agents_for_parent(project_dir, agent_slug, Some(&worktree_path)).await;

    if worktree_path.exists() {
        let wt = git_wt.clone();
        let wt_path = worktree_path.clone();
        match tokio::task::spawn_blocking(move || wt.remove_workspace(&wt_path)).await {
            Ok(Ok(())) => info!(path = %worktree_path.display(), "Removed agent worktree"),
            Ok(Err(e)) => {
                warn!(error = %e, path = %worktree_path.display(), "Failed to remove worktree (non-fatal)")
            }
            Err(e) => warn!(error = %e, "spawn_blocking failed for worktree removal"),
        }
    }

    let agent_dir = project_dir.join(".exo/agents").join(agent_slug);
    if agent_dir.exists() {
        if let Err(e) = std::fs::remove_dir_all(&agent_dir) {
            warn!(error = %e, path = %agent_dir.display(), "Failed to remove agent dir (non-fatal)");
        } else {
            info!(path = %agent_dir.display(), "Removed agent dir");
        }
    }
}

fn agent_routing_dirs(project_dir: &Path, agent_slug: &str, worktree_path: &Path) -> Vec<PathBuf> {
    vec![
        project_dir.join(".exo/agents").join(agent_slug),
        worktree_path.to_path_buf(),
    ]
}

async fn close_agent_tmux_window(project_dir: &Path, agent_slug: &str, worktree_path: &Path) {
    for routing_dir in agent_routing_dirs(project_dir, agent_slug, worktree_path) {
        let Ok(routing) = RoutingInfo::read_from_dir(&routing_dir).await else {
            continue;
        };
        let Some(window_id) = routing.window_id else {
            debug!(path = %routing_dir.display(), agent = agent_slug, "Agent routing has no tmux window id");
            continue;
        };

        let tmux = TmuxIpc::new("");
        match tmux.kill_window(&window_id).await {
            Ok(()) => {
                info!(agent = agent_slug, window = %window_id, path = %routing_dir.display(), "Closed agent tmux window before worktree removal");
                return;
            }
            Err(error) => {
                warn!(agent = agent_slug, window = %window_id, path = %routing_dir.display(), error = %error, "Failed to close agent tmux window before worktree removal");
            }
        }
    }
}

async fn cleanup_worker_agents_for_parent(
    project_dir: &Path,
    parent_slug: &str,
    worktree_path: Option<&Path>,
) {
    let mut agents_dirs = vec![project_dir.join(".exo/agents")];
    if let Some(worktree_path) = worktree_path {
        agents_dirs.push(worktree_path.join(".exo/agents"));
    }

    for agents_dir in agents_dirs {
        cleanup_worker_agents_in_dir(&agents_dir, parent_slug).await;
    }
}

async fn cleanup_worker_agents_in_dir(agents_dir: &Path, parent_slug: &str) {
    let Ok(mut entries) = tokio::fs::read_dir(agents_dir).await else {
        return;
    };

    while let Ok(Some(entry)) = entries.next_entry().await {
        let Ok(file_type) = entry.file_type().await else {
            continue;
        };
        if !file_type.is_dir() {
            continue;
        }

        let agent_dir = entry.path();
        let routing_path = agent_dir.join("routing.json");
        let Ok(content) = tokio::fs::read_to_string(&routing_path).await else {
            continue;
        };
        let Ok(routing) = serde_json::from_str::<serde_json::Value>(&content) else {
            continue;
        };
        let parent_tab = routing
            .get("parent_tab")
            .and_then(serde_json::Value::as_str);
        if !parent_tab_matches_slug(parent_tab, parent_slug) {
            continue;
        }

        if let Some(pane_id) = routing.get("pane_id").and_then(serde_json::Value::as_str) {
            match crate::services::tmux_events::close_worker_pane(pane_id).await {
                Ok(()) => info!(pane_id, path = %agent_dir.display(), "Closed child worker pane"),
                Err(e) => {
                    warn!(pane_id, path = %agent_dir.display(), error = %e, "Failed to close child worker pane (non-fatal)")
                }
            }
        }

        if let Err(e) = tokio::fs::remove_dir_all(&agent_dir).await {
            warn!(path = %agent_dir.display(), error = %e, "Failed to remove child worker config dir (non-fatal)");
        } else {
            info!(path = %agent_dir.display(), "Removed child worker config dir");
        }
    }
}

fn parent_tab_matches_slug(parent_tab: Option<&str>, parent_slug: &str) -> bool {
    parent_tab
        .and_then(|tab| tab.split_whitespace().last())
        .is_some_and(|last| last == parent_slug)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_reviewer_slug_for_pr_matches_exact_pr_prefix() {
        assert!(is_reviewer_slug_for_pr("review-pr-12-codex", 12));
        assert!(is_reviewer_slug_for_pr("review-pr-12-opencode", 12));
        assert!(!is_reviewer_slug_for_pr("review-pr-123-codex", 12));
        assert!(!is_reviewer_slug_for_pr("issue-12-codex", 12));
    }

    #[test]
    fn test_parent_tab_matches_agent_slug() {
        assert!(parent_tab_matches_slug(
            Some("agent trivial-contributing-codex"),
            "trivial-contributing-codex"
        ));
        assert!(parent_tab_matches_slug(
            Some("agent review-pr-1-codex"),
            "review-pr-1-codex"
        ));
        assert!(!parent_tab_matches_slug(
            Some("agent other-worker-codex"),
            "trivial-contributing-codex"
        ));
        assert!(!parent_tab_matches_slug(None, "trivial-contributing-codex"));
    }

    #[test]
    fn test_agent_routing_dirs_check_root_config_before_worktree_root() {
        let project_dir = Path::new("/repo");
        let worktree_path = Path::new("/repo/.exo/worktrees/review-pr-11-codex");

        assert_eq!(
            agent_routing_dirs(project_dir, "review-pr-11-codex", worktree_path),
            vec![
                PathBuf::from("/repo/.exo/agents/review-pr-11-codex"),
                PathBuf::from("/repo/.exo/worktrees/review-pr-11-codex"),
            ]
        );
    }
}
