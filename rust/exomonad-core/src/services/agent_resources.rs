use crate::services::git_worktree::GitWorktreeService;
use std::path::Path;
use std::sync::Arc;
use tracing::{info, warn};

pub async fn dispose_agent_resources(
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
    agent_slug: &str,
) {
    let worktree_path = project_dir.join(".exo/worktrees").join(agent_slug);
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
}
