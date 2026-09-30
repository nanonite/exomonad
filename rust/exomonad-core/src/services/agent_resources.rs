use crate::domain::RoutingInfo;
use crate::services::agent_control::codex_lifecycle::{
    self, CapturedCodexTrust, CodexTrustReleaseBatch,
};
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

/// What one permanent agent disposal did with the ExoMonad Codex trust the
/// disposed resources justified.
///
/// A caller that discards this would report plain success for a disposal whose
/// hook trust is still installed, or whose resources are still on disk, so both
/// the removal outcome and the release batch are returned rather than logged and
/// dropped.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AgentResourceDisposal {
    /// The Codex trust claims captured before the resources were removed, kept so
    /// a caller that cannot retry immediately still holds the evidence.
    pub codex_trust_captured: Vec<CapturedCodexTrust>,
    /// The release outcome for those claims.
    pub codex_trust: CodexTrustReleaseBatch,
    /// Managed resources that are still on disk because their removal failed.
    ///
    /// Non-empty means the disposal is only partial: the agent is still there, so
    /// its Codex trust is still justified and was deliberately left installed.
    pub undisposed: Vec<String>,
}

impl AgentResourceDisposal {
    /// True when every managed resource was proven gone.
    pub fn is_disposed(&self) -> bool {
        self.undisposed.is_empty()
    }

    /// True only when the disposal completed *and* left no hook trust behind.
    ///
    /// Both halves matter. A failed removal must not read as a released trust,
    /// because the agent whose trust would have been released is still running
    /// with its generated config in place.
    pub fn released_codex_trust(&self) -> bool {
        self.is_disposed() && self.codex_trust.is_complete()
    }

    /// A single operator-facing description of an incomplete disposal.
    pub fn failure_reason(&self) -> Option<String> {
        if self.is_disposed() && self.codex_trust.is_complete() {
            return None;
        }
        let mut reasons = Vec::new();
        if !self.undisposed.is_empty() {
            reasons.push(format!("still on disk: {}", self.undisposed.join(", ")));
        }
        if !self.codex_trust.is_complete() {
            reasons.push(format!(
                "ExoMonad Codex trust could not be released: {}",
                self.codex_trust.failures.join("; ")
            ));
        }
        Some(reasons.join("; "))
    }
}

pub async fn dispose_agent_resources(
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
    agent_slug: &str,
) -> AgentResourceDisposal {
    let worktree_path = project_dir.join(".exo/worktrees").join(agent_slug);
    let agent_dir = project_dir.join(".exo/agents").join(agent_slug);
    close_agent_tmux_window(project_dir, agent_slug, &worktree_path).await;
    cleanup_worker_agents_for_parent(project_dir, agent_slug, Some(&worktree_path)).await;

    // Claim the Codex trust before removal: the generated config that proves
    // ownership sits inside one of the directories about to be destroyed.
    let codex_trust_captured = codex_lifecycle::capture_codex_trust_for_disposal(&[
        worktree_path.as_path(),
        agent_dir.as_path(),
    ])
    .unwrap_or_else(|error| {
        warn!(
            agent = agent_slug,
            %error,
            "Could not claim the ExoMonad Codex trust for this agent; disposal continues and the \
             Codex user config is left untouched"
        );
        Vec::new()
    });

    let mut undisposed = Vec::new();
    if !remove_agent_worktree(agent_slug, &worktree_path, git_wt.clone()).await {
        undisposed.push(worktree_path.display().to_string());
    }
    if !remove_agent_dir(agent_slug, &agent_dir) {
        undisposed.push(agent_dir.display().to_string());
    }

    // Only a proven disposal releases the trust. A directory that survived is
    // still an owner whose generated config Codex loads, so removing its hook
    // trust would break an agent that was never disposed.
    let codex_trust = if undisposed.is_empty() {
        release_captured_trust(agent_slug, codex_trust_captured.clone()).await
    } else {
        warn!(
            agent = agent_slug,
            undisposed = %undisposed.join(", "),
            "Disposal is partial, so the ExoMonad Codex trust for this agent is left installed"
        );
        CodexTrustReleaseBatch::default()
    };
    AgentResourceDisposal {
        codex_trust_captured,
        codex_trust,
        undisposed,
    }
}

/// Removes one agent worktree, reporting whether it is proven gone.
///
/// A worktree that still exists after the attempt is a failed disposal no matter
/// what the removal call returned, so absence on disk is the only proof accepted.
async fn remove_agent_worktree(
    agent_slug: &str,
    worktree_path: &Path,
    git_wt: Arc<GitWorktreeService>,
) -> bool {
    if worktree_path.exists() {
        let wt = git_wt;
        let wt_path = worktree_path.to_path_buf();
        match tokio::task::spawn_blocking(move || wt.remove_workspace(&wt_path)).await {
            Ok(Ok(())) => info!(path = %worktree_path.display(), "Removed agent worktree"),
            Ok(Err(e)) => {
                warn!(error = %e, path = %worktree_path.display(), "Failed to remove worktree (non-fatal)")
            }
            Err(e) => warn!(error = %e, "spawn_blocking failed for worktree removal"),
        }
    }
    if !worktree_path.exists() {
        return true;
    }
    warn!(
        agent = agent_slug,
        path = %worktree_path.display(),
        "Agent worktree survived removal; treating the disposal as partial"
    );
    false
}

/// Removes one agent configuration directory, reporting whether it is gone.
fn remove_agent_dir(agent_slug: &str, agent_dir: &Path) -> bool {
    if agent_dir.exists() {
        if let Err(e) = std::fs::remove_dir_all(agent_dir) {
            warn!(error = %e, path = %agent_dir.display(), "Failed to remove agent dir (non-fatal)");
        } else {
            info!(path = %agent_dir.display(), "Removed agent dir");
        }
    }
    if !agent_dir.exists() {
        return true;
    }
    warn!(
        agent = agent_slug,
        path = %agent_dir.display(),
        "Agent directory survived removal; treating the disposal as partial"
    );
    false
}

async fn release_captured_trust(
    agent_slug: &str,
    captured: Vec<CapturedCodexTrust>,
) -> CodexTrustReleaseBatch {
    if captured.is_empty() {
        return CodexTrustReleaseBatch::default();
    }
    let batch = tokio::task::spawn_blocking(move || {
        codex_lifecycle::release_captured_codex_trusts(&captured)
    })
    .await
    .unwrap_or_else(|error| CodexTrustReleaseBatch {
        released: Vec::new(),
        failures: vec![format!("Codex trust release task failed: {error}")],
    });
    for release in &batch.released {
        info!(
            agent = agent_slug,
            config = %release.hook_trust,
            "Released ExoMonad Codex trust for a permanently disposed agent"
        );
    }
    if !batch.is_complete() {
        warn!(
            agent = agent_slug,
            failures = %batch.failures.join("; "),
            "Agent resources were disposed but ExoMonad Codex hook trust survived; release it \
             before retrying so the claim is not lost"
        );
    }
    batch
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

        // Claimed before the removal: the generated Codex config that proves
        // ownership is inside the directory being destroyed.
        let child_trust = codex_lifecycle::capture_codex_trust_for_disposal(&[agent_dir.as_path()])
            .unwrap_or_else(|error| {
                warn!(
                    path = %agent_dir.display(),
                    %error,
                    "Could not claim the ExoMonad Codex trust for this child worker; the Codex user \
                     config is left untouched"
                );
                Vec::new()
            });
        if let Err(e) = tokio::fs::remove_dir_all(&agent_dir).await {
            warn!(path = %agent_dir.display(), error = %e, "Failed to remove child worker config dir (non-fatal)");
        } else {
            info!(path = %agent_dir.display(), "Removed child worker config dir");
            // A child worker is permanently disposed once its config dir is gone,
            // so the Codex trust it needed is no longer justified.
            release_captured_trust(&agent_dir.display().to_string(), child_trust).await;
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
        let disposal = dispose_agent_resources(project_dir, git_wt.clone(), slug).await;
        if let Some(reason) = disposal.failure_reason() {
            warn!(
                reviewer = %slug,
                %reason,
                "Reviewer disposal did not complete; ExoMonad Codex hook trust is left installed"
            );
        }
    }
    slugs
}

#[cfg(test)]
mod orphan_cleanup_tests {
    use super::*;
    use crate::services::agent_control::codex_lifecycle::test_support::IsolatedCodex;
    use serial_test::serial;
    use std::os::unix::fs::PermissionsExt;

    fn invocation(status: &str, ended_at: Option<u64>) -> serde_json::Value {
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
    }

    /// A dormant reviewer is still resumable, so its Codex trust must survive
    /// the reconciler even though its sibling was disposed.
    #[tokio::test]
    #[serial]
    async fn orphan_cleanup_releases_only_a_disposed_reviewers_codex_trust() {
        let codex = IsolatedCodex::default();
        let temp_dir = tempfile::tempdir().unwrap();
        let exited_slug = "review-pr-1-codex";
        let live_slug = "review-pr-2-codex";
        let exited_dir = temp_dir.path().join(".exo/agents").join(exited_slug);
        let live_dir = temp_dir.path().join(".exo/agents").join(live_slug);
        tokio::fs::create_dir_all(&exited_dir).await.unwrap();
        tokio::fs::create_dir_all(&live_dir).await.unwrap();
        codex.provision_for_role(&exited_dir, exited_slug, "reviewer");
        codex.provision_for_role(&live_dir, live_slug, "reviewer");
        assert_eq!(codex.hook_trust_entries(), 6, "both reviewers are trusted");

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
        assert!(!exited_dir.exists(), "the exited reviewer is disposed");
        assert!(live_dir.exists(), "the live reviewer is untouched");
        assert_eq!(
            codex.hook_trust_entries(),
            3,
            "only the disposed reviewer's hook trust may be removed"
        );
        let live_config = live_dir.join(".codex/config.toml").display().to_string();
        assert!(
            codex
                .hook_trust_keys()
                .iter()
                .all(|key| key.starts_with(&live_config)),
            "the dormant reviewer keeps every trust record it was provisioned with"
        );
    }

    /// A worktree that survives removal must keep its trust, because the agent it
    /// belongs to is still on disk and still reading its generated config.
    #[tokio::test]
    #[serial]
    async fn a_failed_worktree_removal_keeps_the_codex_trust_installed() {
        let codex = IsolatedCodex::default();
        let temp_dir = tempfile::tempdir().unwrap();
        let slug = "issue-42-leaf-codex";
        let worktree = temp_dir.path().join(".exo/worktrees").join(slug);
        tokio::fs::create_dir_all(&worktree).await.unwrap();
        codex.provision(&worktree, slug);
        assert_eq!(codex.hook_trust_entries(), 3);
        // Read-only: `git worktree remove` cannot unlink anything and the manual
        // `remove_dir_all` fallback cannot either, so the disposal is partial.
        set_directory_read_only(&worktree);

        let disposal = dispose_agent_resources(
            temp_dir.path(),
            Arc::new(GitWorktreeService::new(temp_dir.path().to_path_buf())),
            slug,
        )
        .await;

        set_directory_writable(&worktree);
        assert!(
            !disposal.is_disposed(),
            "the surviving worktree must be reported as undisposed"
        );
        assert!(
            !disposal.released_codex_trust(),
            "a partial disposal must never read as a released trust: {:?}",
            disposal
        );
        assert!(
            disposal
                .failure_reason()
                .is_some_and(|reason| reason.contains("still on disk")),
            "the operator must be told what survived: {:?}",
            disposal.failure_reason()
        );
        assert!(
            worktree.exists(),
            "the worktree holding the generated config is still there"
        );
        assert_eq!(
            codex.hook_trust_entries(),
            3,
            "an owner that was not disposed stays resumable, so its trust must stay"
        );
        assert!(
            disposal.codex_trust.released.is_empty(),
            "nothing may be released while the owner is still on disk"
        );
    }

    /// An agent directory that survives removal must keep its trust too.
    #[tokio::test]
    #[serial]
    async fn a_failed_agent_directory_removal_keeps_the_codex_trust_installed() {
        let codex = IsolatedCodex::default();
        let temp_dir = tempfile::tempdir().unwrap();
        let slug = "issue-42-leaf-codex";
        let agent_dir = temp_dir.path().join(".exo/agents").join(slug);
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();
        codex.provision(&agent_dir, slug);
        assert_eq!(codex.hook_trust_entries(), 3);
        set_directory_read_only(&agent_dir);

        let disposal = dispose_agent_resources(
            temp_dir.path(),
            Arc::new(GitWorktreeService::new(temp_dir.path().to_path_buf())),
            slug,
        )
        .await;

        set_directory_writable(&agent_dir);
        assert!(!disposal.is_disposed());
        assert!(!disposal.released_codex_trust());
        assert!(agent_dir.exists());
        assert_eq!(
            codex.hook_trust_entries(),
            3,
            "a config Codex still loads must keep its trust"
        );
    }

    fn set_directory_read_only(path: &Path) {
        let mut permissions = std::fs::metadata(path).unwrap().permissions();
        permissions.set_mode(0o500);
        std::fs::set_permissions(path, permissions).unwrap();
    }

    fn set_directory_writable(path: &Path) {
        let mut permissions = std::fs::metadata(path).unwrap().permissions();
        permissions.set_mode(0o700);
        std::fs::set_permissions(path, permissions).unwrap();
    }

    /// A permanent disposal reports the trust it released, so a caller can never
    /// mistake a partial disposal for a clean one.
    #[tokio::test]
    #[serial]
    async fn disposing_an_agent_reports_the_codex_trust_it_released() {
        let codex = IsolatedCodex::default();
        let temp_dir = tempfile::tempdir().unwrap();
        let slug = "issue-42-leaf-codex";
        let agent_dir = temp_dir.path().join(".exo/agents").join(slug);
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();
        codex.provision(&agent_dir, "issue-42-leaf-codex");

        let disposal = dispose_agent_resources(
            temp_dir.path(),
            Arc::new(GitWorktreeService::new(temp_dir.path().to_path_buf())),
            slug,
        )
        .await;

        assert!(disposal.released_codex_trust(), "{disposal:?}");
        assert!(disposal.is_disposed(), "{disposal:?}");
        assert!(disposal.failure_reason().is_none());
        assert_eq!(disposal.codex_trust_captured.len(), 1);
        assert_eq!(disposal.codex_trust.released[0].hook_trust.removed.len(), 3);
        assert!(!agent_dir.exists());
        assert_eq!(
            codex.hook_trust_entries(),
            0,
            "a proven disposal must leave no ExoMonad hook trust behind"
        );
    }

    /// An agent that was never a Codex agent has no trust to release, and the
    /// disposal must not reach into the Codex user config looking for some.
    #[tokio::test]
    #[serial]
    async fn disposing_a_non_codex_agent_claims_nothing() {
        let _codex = IsolatedCodex::default();
        let temp_dir = tempfile::tempdir().unwrap();
        let slug = "issue-42-worker-opencode";
        let agent_dir = temp_dir.path().join(".exo/agents").join(slug);
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();

        let disposal = dispose_agent_resources(
            temp_dir.path(),
            Arc::new(GitWorktreeService::new(temp_dir.path().to_path_buf())),
            slug,
        )
        .await;

        assert!(disposal.released_codex_trust(), "{:?}", disposal);
        assert!(disposal.is_disposed());
        assert!(disposal.failure_reason().is_none());
        assert!(disposal.codex_trust_captured.is_empty());
        assert!(disposal.codex_trust.released.is_empty());
        assert!(disposal.undisposed.is_empty());
        assert!(!agent_dir.exists());
    }
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
