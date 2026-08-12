use crate::domain::RoutingInfo;
use crate::services::agent_control::read_invocation;
use crate::services::git_worktree::GitWorktreeService;
use crate::services::tmux_ipc::TmuxIpc;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tracing::{debug, info, warn};

fn reviewer_pr_number(slug: &str) -> Option<u64> {
    let rest = slug.strip_prefix("review-pr-")?;
    let (digits, suffix) = rest.split_once('-')?;
    if digits.is_empty() || suffix.is_empty() {
        return None;
    }
    digits.parse().ok()
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

/// Dispose reviewer resources whose latest invocation is terminal.
///
/// Cleanup is intentionally resource-driven: an exited invocation proves that
/// its tmux process no longer owns the reviewer worktree. Forgejo verdicts are
/// observations and do not trigger this reconciler.
pub async fn dispose_exited_reviewer_resources(
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
) -> Vec<String> {
    let agents_dir = project_dir.join(".exo/agents");
    let Ok(mut entries) = tokio::fs::read_dir(&agents_dir).await else {
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
        let Some(slug) = entry.file_name().to_str().map(str::to_string) else {
            continue;
        };
        if reviewer_pr_number(&slug).is_none() {
            continue;
        }
        let invocation = match read_invocation(&entry.path()).await {
            Ok(invocation) => invocation,
            Err(error) => {
                warn!(agent = %slug, %error, "Skipping reviewer cleanup with malformed invocation metadata");
                continue;
            }
        };
        let Some(invocation) = invocation else {
            continue;
        };
        if invocation.is_live() {
            continue;
        }
        slugs.push(slug);
    }

    slugs.sort();
    for slug in &slugs {
        info!(reviewer = %slug, "Disposing exited reviewer agent");
        dispose_agent_resources(project_dir, git_wt.clone(), slug).await;
    }
    slugs
}

#[cfg(test)]
#[tokio::test]
async fn dispose_exited_reviewer_resources_preserves_live_reviewers() {
    let temp_dir = tempfile::tempdir().unwrap();
    let exited_slug = "review-pr-1-codex";
    let live_slug = "review-pr-2-codex";
    let exited_dir = temp_dir.path().join(".exo/agents").join(exited_slug);
    let live_dir = temp_dir.path().join(".exo/agents").join(live_slug);
    tokio::fs::create_dir_all(&exited_dir).await.unwrap();
    tokio::fs::create_dir_all(&live_dir).await.unwrap();

    let invocation = |status: &str, ended_at: Option<u64>| {
        serde_json::json!({
            "invocation_id": status,
            "runtime": "codex",
            "trigger": "review",
            "routing": {"window_id": null, "pane_id": null, "parent_tab": null},
            "started_at": 1,
            "ended_at": ended_at,
            "status": status,
            "exit_code": 0,
            "pr_number": 1,
            "head_sha": "abc123",
            "generation": 1
        })
    };
    tokio::fs::write(
        exited_dir.join("invocation.json"),
        serde_json::to_vec(&invocation("exited", Some(2))).unwrap(),
    )
    .await
    .unwrap();
    tokio::fs::write(
        live_dir.join("invocation.json"),
        serde_json::to_vec(&invocation("running", None)).unwrap(),
    )
    .await
    .unwrap();

    let cleaned = dispose_exited_reviewer_resources(
        temp_dir.path(),
        Arc::new(GitWorktreeService::new(temp_dir.path().to_path_buf())),
    )
    .await;

    assert_eq!(cleaned, vec![exited_slug]);
    assert!(!exited_dir.exists());
    assert!(live_dir.exists());
}
