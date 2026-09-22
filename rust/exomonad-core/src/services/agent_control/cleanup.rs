use super::*;

impl<
        C: super::super::HasGitHubClient
            + super::super::HasTeamRegistry
            + super::super::HasAgentResolver
            + super::super::HasProjectDir
            + super::super::HasGitWorktreeService
            + 'static,
    > AgentControlService<C>
{
    pub(crate) async fn routing_liveness(&self, agent_dir: &Path) -> Option<bool> {
        if !agent_dir.is_dir() {
            return None;
        }
        if agent_dir.join("exited_at").exists() {
            return Some(false);
        }
        let invocation_routing = match invocation::read_invocation(agent_dir).await {
            Ok(Some(invocation)) if invocation.is_live() => Some(invocation.routing),
            Ok(Some(_)) => return Some(false),
            Ok(None) => None,
            Err(error) => {
                warn!(
                    path = %agent_dir.display(),
                    %error,
                    "Could not read invocation metadata for liveness"
                );
                return Some(false);
            }
        };
        if agent_dir.join("exit_code").exists() {
            debug!(
                path = %agent_dir.display(),
                "Agent runtime exit marker is present; treating routing as dead"
            );
            return Some(false);
        }
        let routing_path = agent_dir.join("routing.json");
        let routing = match RoutingInfo::read_from_dir(agent_dir).await {
            Ok(routing) => routing,
            Err(error) => {
                if let Some(invocation_routing) = invocation_routing.clone() {
                    if routing_path.exists() {
                        warn!(
                            path = %agent_dir.display(),
                            %error,
                            "Using routing captured by current invocation metadata because routing.json is unreadable"
                        );
                    } else {
                        debug!(
                            path = %agent_dir.display(),
                            %error,
                            "Using routing captured by current invocation metadata because routing.json is unavailable"
                        );
                    }
                    invocation_routing
                } else {
                    warn!(
                        path = %agent_dir.display(),
                        %error,
                        "Could not read agent routing for liveness"
                    );
                    return Some(false);
                }
            }
        };
        if !routing.has_delivery_target() {
            warn!(path = %agent_dir.display(), "Agent routing has no live target");
            return Some(false);
        }
        let tmux = match self.tmux() {
            Ok(tmux) => tmux,
            Err(error) => {
                warn!(path = %agent_dir.display(), error = %error, "Could not create tmux client for agent liveness");
                return Some(false);
            }
        };
        let target_alive = crate::services::tmux_ipc::routing_target_alive(&routing, &tmux)
            .await
            .unwrap_or_else(|error| {
                warn!(path = %agent_dir.display(), error = %error, "Routing liveness check failed");
                false
            });
        if !target_alive {
            return Some(false);
        }
        Some(
            tmux.routing_target_process_alive(&routing)
                .await
                .unwrap_or_else(|error| {
                    warn!(path = %agent_dir.display(), error = %error, "Routing process liveness check failed");
                    false
                }),
        )
    }

    async fn activity_marker(&self, agent_dir: &Path) -> Option<u64> {
        tokio::fs::read_to_string(agent_dir.join(LAST_ACTIVITY_FILE))
            .await
            .ok()?
            .trim()
            .parse()
            .ok()
    }

    /// Clean up an agent by identifier (internal_name or issue_id).
    ///
    /// Kills the tmux window, unregisters from Teams config.json,
    /// and removes per-agent config directory (`.exo/agents/{name}/`).
    #[tracing::instrument(skip(self))]
    pub async fn cleanup_agent(&self, identifier: &str) -> Result<()> {
        // Try to find agent in list (for metadata and window matching).
        // Failure here is non-fatal to allow cleaning up worker panes (invisible to list_agents).
        let agents = self.list_agents().await.unwrap_or_default();
        let agent = agents
            .iter()
            .find(|a| a.internal_name.as_str() == identifier);

        info!(
            identifier,
            found = agent.is_some(),
            "Initiating cleanup_agent"
        );

        // Parse identifier into AgentIdentity to get consistent slug/internal_name/display_name.
        // Try resolver first for authoritative identity, then fall back to derivation.
        let identity = {
            let resolver = self.agent_resolver();
            let agent_name_key =
                AgentName::try_from_str(identifier).expect("validated string input is non-empty");
            if let Some(record) = resolver.get(&agent_name_key).await {
                let slug = record.slug.as_str().to_string();
                AgentIdentity::new(slug, record.agent_type)
            } else {
                AgentIdentity::from_internal_name(identifier)
            }
        };

        // Remove synthetic team member registration (non-fatal if not registered).
        // Synthetic members are registered under internal_name (e.g., "beta-claude").
        {
            let team_reg = self.team_registry();
            let birth_branch_str = self.birth_branch.as_str();
            let team_info = if let Some(info) = team_reg.get(birth_branch_str).await {
                Some(info)
            } else if let Some(parent) = self.birth_branch.parent() {
                team_reg.get(parent.as_str()).await
            } else {
                None
            };
            let member_name = identity.internal_name();
            if let Some(info) = team_info {
                let team_name = TeamName::try_from_str(info.team_name.as_str())
                    .expect("validated string input is non-empty");
                if let Err(e) = crate::services::synthetic_members::remove_synthetic_member(
                    &team_name,
                    &member_name,
                ) {
                    warn!(team = %team_name, member = %member_name, error = %e, "Failed to remove synthetic team member (non-fatal)");
                }
            } else {
                debug!(member = %member_name, "No team found in registry — skipping synthetic member removal");
            }
        }

        let internal_name = identity.internal_name();
        let display_name = Some(identity.display_name());

        // Remove per-agent config directory (.exo/agents/{name}/)
        let agent_config_dir = self
            .project_dir()
            .join(".exo")
            .join("agents")
            .join(internal_name.as_str());
        let worktree_config_dir = self
            .project_dir()
            .join(".exo")
            .join("worktrees")
            .join(internal_name.as_str());

        // Try direct cleanup via stored window_id (O(1), no listing needed)
        let mut window_closed = false;
        for routing_dir in [&agent_config_dir, &worktree_config_dir] {
            if let Ok(routing) = RoutingInfo::read_from_dir(routing_dir).await {
                if let Some(wid) = routing.window_id {
                    let tmux = self.tmux()?;
                    match tmux.kill_window(&wid).await {
                        Ok(()) => {
                            info!(identifier, path = %routing_dir.display(), "Closed tmux window via stored window_id");
                            window_closed = true;
                            break;
                        }
                        Err(e) => {
                            warn!(identifier, path = %routing_dir.display(), error = %e, "kill_window by stored ID failed, falling back to name match");
                        }
                    }
                }
            }
        }

        // Close tmux window if found in list
        if !window_closed {
            if let Some(target_window) = display_name {
                let windows = self.get_tmux_windows().await.unwrap_or_default();
                for window in &windows {
                    if window == &target_window {
                        if let Err(e) = self.close_tmux_window(window).await {
                            warn!(window_name = %window, error = %e, "Failed to close tmux window (may not exist)");
                        }
                        break;
                    }
                }
            }
        }

        // Remove git worktree if it exists.
        // spawn_subtree/spawn_leaf_subtree use bare slug as dir name,
        // spawn_agent uses internal_name ({id}-{type}).
        let worktree_path = {
            let slug_path = self.worktree_base.join(identity.slug());
            if slug_path.exists() {
                slug_path
            } else {
                self.worktree_base.join(internal_name.as_str())
            }
        };
        if worktree_path.exists() {
            let git_wt = self.git_wt().clone();
            let path = worktree_path.clone();
            let join_result =
                tokio::task::spawn_blocking(move || git_wt.remove_workspace(&path)).await;
            match join_result {
                Ok(Ok(())) => {
                    // Successfully removed workspace
                }
                Ok(Err(e)) => {
                    return Err(anyhow::anyhow!(
                        "failed to remove git worktree {}: {e}",
                        worktree_path.display()
                    ));
                }
                Err(join_err) => {
                    return Err(anyhow::anyhow!(
                        "git worktree removal task failed for {}: {join_err}",
                        worktree_path.display()
                    ));
                }
            }
        }

        if agent_config_dir.exists() {
            if let Err(e) = fs::remove_dir_all(&agent_config_dir).await {
                return Err(anyhow::anyhow!(
                    "failed to remove per-agent config dir {}: {e}",
                    agent_config_dir.display()
                ));
            }
            info!(path = %agent_config_dir.display(), "Removed per-agent config dir");
        }

        // Deregister identity from resolver
        {
            let resolver = self.agent_resolver();
            if let Err(e) = resolver.deregister(&internal_name).await {
                warn!(agent = %internal_name, error = %e, "Failed to deregister agent identity (non-fatal)");
            }
        }

        // Emit agent:stopped event
        if let Some(ref session) = self.tmux_session {
            if let Ok(agent_id) = crate::ui_protocol::AgentId::try_from(identifier.to_string()) {
                let event = crate::ui_protocol::AgentEvent::AgentStopped {
                    agent_id,
                    timestamp: tmux_events::now_iso8601(),
                };
                if let Err(e) = tmux_events::emit_event(session, &event) {
                    warn!("Failed to emit agent:stopped event: {}", e);
                }
            }
        }

        Ok(())
    }

    /// Clean up multiple agents.
    #[tracing::instrument(skip(self))]
    pub async fn cleanup_agents(
        &self,
        issue_ids: &[String],
        _subrepo: Option<&str>,
    ) -> BatchCleanupResult {
        let mut result = BatchCleanupResult {
            cleaned: Vec::new(),
            failed: Vec::new(),
        };

        for issue_id in issue_ids {
            match self.cleanup_agent(issue_id).await {
                Ok(()) => result.cleaned.push(issue_id.clone()),
                Err(e) => {
                    warn!(issue_id, error = %e, "Failed to cleanup agent");
                    result.failed.push((issue_id.clone(), e.to_string()));
                }
            }
        }

        result
    }

    /// List all active agents by scanning the filesystem and verifying with tmux.
    ///
    /// Discovery process:
    /// 1. Scan {worktree_base}/ for subtree agents (isolated worktrees)
    /// 2. Scan {project_dir}/.exo/agents/ for worker agents (shared worktree)
    /// 3. Verify liveness by checking tmux windows/panes
    #[tracing::instrument(skip(self))]
    pub async fn list_agents(&self) -> Result<Vec<AgentInfo>> {
        let mut agents = Vec::new();

        // Get all tmux windows for liveness check
        let windows = self.get_tmux_windows().await.unwrap_or_default();

        // 1. Scan worktree_base for subtree agents
        if self.worktree_base.exists() {
            let mut entries = fs::read_dir(&self.worktree_base).await?;
            while let Some(entry) = entries.next_entry().await? {
                if entry.file_type().await?.is_dir() {
                    let path = entry.path();
                    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");

                    // Agent configs are the discovery markers; routing.json and
                    // invocation metadata remain the liveness authority.
                    let is_claude = path.join(".mcp.json").exists();
                    let is_shoal = path.join(".exo/mcp.json").exists();
                    let is_opencode = path.join("opencode.json").exists();
                    let is_codex = path.join(".codex/config.toml").exists();

                    if is_claude || is_shoal || is_opencode || is_codex {
                        let inferred_type = AgentType::from_dir_name(name);
                        let agent_type = if is_shoal {
                            AgentType::Shoal
                        } else if is_opencode {
                            AgentType::OpenCode
                        } else if is_codex {
                            AgentType::Codex
                        } else if is_claude {
                            AgentType::Claude
                        } else {
                            inferred_type
                        };
                        let suffix = format!("-{}", agent_type.suffix());
                        let slug_str = name.strip_suffix(&suffix).unwrap_or(name);
                        let display_name = format!("{} {}", agent_type.emoji(), slug_str);

                        let config_dir = self.project_dir().join(".exo/agents").join(name);
                        let display_alive = windows.iter().any(|t| t == &display_name);
                        let has_tab = self
                            .routing_liveness(&config_dir)
                            .await
                            .unwrap_or(display_alive);
                        let last_activity_at = self.activity_marker(&config_dir).await;

                        agents.push(AgentInfo {
                            internal_name: AgentName::try_from_str(name)
                                .expect("validated string input is non-empty"),
                            has_tab,
                            topology: Topology::WorktreePerAgent,
                            agent_dir: Some(config_dir.clone()),
                            worktree_path: Some(path.clone()),
                            slug: Some(
                                AgentName::try_from_str(slug_str)
                                    .expect("validated string input is non-empty"),
                            ),
                            agent_type: Some(agent_type),
                            pr: None,
                            last_activity_at,
                        });

                        // 2. Scan subtree's .exo/agents for workers
                        let subtree_agents_dir = path.join(".exo/agents");
                        if subtree_agents_dir.exists() {
                            self.scan_workers(&subtree_agents_dir, &windows, &mut agents)
                                .await?;
                        }
                    }
                }
            }
        }

        // 3. Scan root .exo/agents for workers
        let root_agents_dir = self.project_dir().join(".exo/agents");
        if root_agents_dir.exists() {
            self.scan_workers(&root_agents_dir, &windows, &mut agents)
                .await?;
        }

        Ok(agents)
    }

    /// Helper to scan a directory for worker agents.
    pub(crate) async fn scan_workers(
        &self,
        dir: &Path,
        windows: &[String],
        agents: &mut Vec<AgentInfo>,
    ) -> Result<()> {
        let mut entries = fs::read_dir(dir).await?;
        while let Some(entry) = entries.next_entry().await? {
            if entry.file_type().await?.is_dir() {
                let path = entry.path();
                let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");

                let agent_type = AgentType::from_dir_name(name);
                let Some(base_name) = ["-claude", "-shoal", "-opencode", "-codex"]
                    .iter()
                    .find_map(|suffix| name.strip_suffix(suffix))
                else {
                    continue;
                };

                // Skip if this is actually a worktree-based agent (leaf subtree or teammate)
                // found by the worktree scan.
                if agents
                    .iter()
                    .any(|a| a.slug.as_ref().map(|s| s.as_str()) == Some(base_name))
                {
                    continue;
                }

                let display_name = format!("{} {}", agent_type.emoji(), base_name);

                // Liveness: for workers, they might be panes in a window.
                // Currently list_agents only sees windows.
                let display_alive = windows.iter().any(|t| t == &display_name);
                let has_tab = self.routing_liveness(&path).await.unwrap_or(display_alive);
                let last_activity_at = self.activity_marker(&path).await;

                agents.push(AgentInfo {
                    internal_name: AgentName::try_from_str(name)
                        .expect("validated string input is non-empty"),
                    has_tab,
                    topology: Topology::SharedDir,
                    agent_dir: Some(path.clone()),
                    worktree_path: None,
                    slug: Some(
                        AgentName::try_from_str(base_name)
                            .expect("validated string input is non-empty"),
                    ),
                    agent_type: Some(agent_type),
                    pr: None,
                    last_activity_at,
                });
            }
        }
        Ok(())
    }
}

const WORKTREE_SINK_ARTIFACTS: &[&str] = &[
    "logs",
    "events",
    "ledger",
    "analysis",
    "sink-health.json",
    "sink-health.lock",
    "session.json",
];

fn exo_contains_only_sink_artifacts(path: &Path) -> bool {
    let Ok(entries) = std::fs::read_dir(path) else {
        return false;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        if !WORKTREE_SINK_ARTIFACTS.contains(&name.to_string_lossy().as_ref()) {
            return false;
        }
    }
    true
}

fn contains_only_sink_artifacts(path: &Path) -> bool {
    let Ok(entries) = std::fs::read_dir(path) else {
        return false;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        if name.to_string_lossy() != ".exo" {
            return false;
        }
        if !exo_contains_only_sink_artifacts(&entry.path()) {
            return false;
        }
    }
    true
}

fn worktree_name_is_identified(project_dir: &Path, name: &str) -> bool {
    let agents_dir = project_dir.join(".exo/agents");
    let Ok(entries) = std::fs::read_dir(&agents_dir) else {
        return false;
    };
    let needle = format!("worktrees/{name}");
    for entry in entries.flatten() {
        let identity_path = entry.path().join("identity.json");
        if let Ok(contents) = std::fs::read_to_string(&identity_path) {
            if contents.contains(&needle) {
                return true;
            }
        }
    }
    false
}

fn residue_quarantine_name(name: &str) -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    format!("{name}-{nanos}-{}", COUNTER.fetch_add(1, Ordering::Relaxed))
}

/// Quarantine `.exo/worktrees/*` residue directories that are provably disposable.
///
/// Only directories that Git's worktree registry does not know, carry no agent
/// identity, and contain nothing but known sink artifacts are moved into the
/// project-owned `.exo/worktrees-residue/` quarantine. Registered, dirty,
/// identified, or ambiguous directories are left untouched, and every record in
/// a quarantined directory is preserved rather than deleted. Returns the
/// original residue paths that were moved.
pub(crate) fn cleanup_unregistered_worktree_residue(
    project_dir: &Path,
    git_wt: &GitWorktreeService,
) -> Vec<PathBuf> {
    let worktrees_dir = project_dir.join(".exo/worktrees");
    let Ok(entries) = std::fs::read_dir(&worktrees_dir) else {
        return Vec::new();
    };
    let quarantine_root = project_dir.join(".exo/worktrees-residue");
    let mut moved = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        // Git's registry is authoritative: a registered worktree is never
        // residue. An unreadable registry fails closed by skipping cleanup.
        if git_wt.is_registered_worktree(&path).unwrap_or(true) {
            continue;
        }
        let name = entry.file_name();
        let name = name.to_string_lossy().to_string();
        if worktree_name_is_identified(project_dir, &name) {
            continue;
        }
        if !contains_only_sink_artifacts(&path) {
            continue;
        }
        if std::fs::create_dir_all(&quarantine_root).is_err() {
            continue;
        }
        let destination = quarantine_root.join(residue_quarantine_name(&name));
        if std::fs::rename(&path, &destination).is_ok() {
            moved.push(path);
        }
    }
    moved
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch, Slug};
    use crate::services::agent_control::{AgentType, Topology};
    use crate::services::agent_resolver::{AgentIdentityRecord, AgentResolver};
    use crate::services::git_worktree::GitWorktreeService;
    use crate::services::Services;
    use std::os::unix::fs::PermissionsExt;
    use std::sync::Arc;

    fn init_residue_repo() -> (tempfile::TempDir, PathBuf, GitWorktreeService) {
        let temp = tempfile::tempdir().unwrap();
        let project = temp.path().to_path_buf();
        exomonad_test_support::init_fixture_git_repository(&project).unwrap();
        exomonad_test_support::run_fixture_git_command(
            &project,
            &["config", "user.email", "residue@example.invalid"],
        )
        .unwrap();
        exomonad_test_support::run_fixture_git_command(
            &project,
            &["config", "user.name", "Residue Test"],
        )
        .unwrap();
        exomonad_test_support::run_fixture_git_command(
            &project,
            &["commit", "--allow-empty", "-m", "initial"],
        )
        .unwrap();
        let git_wt = GitWorktreeService::new(project.clone());
        (temp, project, git_wt)
    }

    fn fixture_branch(project: &Path) -> String {
        let output =
            exomonad_test_support::run_fixture_git_command(project, &["branch", "--show-current"])
                .unwrap();
        String::from_utf8_lossy(&output.stdout).trim().to_string()
    }

    #[test]
    fn residue_cleanup_quarantines_sink_only_directory() {
        let (_temp, project, git_wt) = init_residue_repo();
        let residue = project.join(".exo/worktrees/leaf-codex");
        std::fs::create_dir_all(residue.join(".exo/ledger/segments")).unwrap();
        std::fs::write(residue.join(".exo/ledger/segments/segment-0.jsonl"), "{}\n").unwrap();

        let moved = cleanup_unregistered_worktree_residue(&project, &git_wt);

        assert_eq!(moved, vec![residue.clone()]);
        assert!(!residue.exists());
        let quarantined: Vec<_> = std::fs::read_dir(project.join(".exo/worktrees-residue"))
            .unwrap()
            .flatten()
            .collect();
        assert_eq!(quarantined.len(), 1);
        assert!(quarantined[0]
            .path()
            .join(".exo/ledger/segments/segment-0.jsonl")
            .is_file());
    }

    #[test]
    fn residue_cleanup_refuses_registered_worktree() {
        let (_temp, project, git_wt) = init_residue_repo();
        let default_branch = fixture_branch(&project);
        let worktree = project.join(".exo/worktrees/leaf-codex");
        let branch =
            crate::domain::BranchName::try_from_str(format!("{default_branch}.leaf").as_str())
                .unwrap();
        let base = crate::domain::BranchName::try_from_str(default_branch.as_str()).unwrap();
        git_wt.create_workspace(&worktree, &branch, &base).unwrap();
        std::fs::create_dir_all(worktree.join(".exo/ledger/segments")).unwrap();

        assert!(cleanup_unregistered_worktree_residue(&project, &git_wt).is_empty());
        assert!(worktree.exists());
    }

    #[test]
    fn residue_cleanup_refuses_identified_worktree() {
        let (_temp, project, git_wt) = init_residue_repo();
        let residue = project.join(".exo/worktrees/leaf-codex");
        std::fs::create_dir_all(residue.join(".exo/logs")).unwrap();
        let agent_dir = project.join(".exo/agents/leaf");
        std::fs::create_dir_all(&agent_dir).unwrap();
        std::fs::write(
            agent_dir.join("identity.json"),
            r#"{"working_dir":".exo/worktrees/leaf-codex/"}"#,
        )
        .unwrap();

        assert!(cleanup_unregistered_worktree_residue(&project, &git_wt).is_empty());
        assert!(residue.exists());
    }

    #[test]
    fn residue_cleanup_refuses_dirty_or_ambiguous_directory() {
        let (_temp, project, git_wt) = init_residue_repo();
        let residue = project.join(".exo/worktrees/leaf-codex");
        std::fs::create_dir_all(residue.join(".exo/logs")).unwrap();
        std::fs::write(residue.join("seed"), "real work\n").unwrap();

        assert!(cleanup_unregistered_worktree_residue(&project, &git_wt).is_empty());
        assert!(residue.exists());
    }

    #[tokio::test]
    async fn cleanup_agent_retains_identity_when_worktree_removal_fails() {
        let temp = tempfile::tempdir().unwrap();
        let project = temp.path().to_path_buf();
        let mut services = Services::test();
        services.project_dir = project.clone();
        let resolver = Arc::new(AgentResolver::load(project.clone()).await);
        services.agent_resolver = resolver.clone();
        services.git_wt = Arc::new(GitWorktreeService::new(project.clone()));
        let service = AgentControlService::new(Arc::new(services));
        let agent_name = AgentName::try_from_str("stale-codex").unwrap();
        let record = AgentIdentityRecord {
            agent_name: agent_name.clone(),
            slug: Slug::try_from_str("stale").unwrap(),
            agent_type: AgentType::Codex,
            birth_branch: BirthBranch::try_from_str("main.stale").unwrap(),
            parent_branch: BirthBranch::try_from_str("main").unwrap(),
            working_dir: ".exo/worktrees/stale".into(),
            display_name: "🤖 stale-codex".to_string(),
            topology: Topology::WorktreePerAgent,
            model: None,
            effort: None,
            ledger_owned: false,
            slice_id: None,
        };
        resolver.register(record).await.unwrap();

        let worktree = project.join(".exo/worktrees/stale");
        tokio::fs::create_dir_all(&worktree).await.unwrap();
        tokio::fs::write(worktree.join("residual"), "must remain")
            .await
            .unwrap();
        tokio::fs::set_permissions(&worktree, std::fs::Permissions::from_mode(0o555))
            .await
            .unwrap();

        let error = service
            .cleanup_agent(agent_name.as_str())
            .await
            .expect_err("failed worktree removal must stop teardown");
        assert!(error.to_string().contains("failed to remove git worktree"));
        assert!(tokio::fs::symlink_metadata(&worktree).await.is_ok());
        assert!(resolver.get(&agent_name).await.is_some());
        assert!(
            tokio::fs::symlink_metadata(project.join(".exo/agents/stale-codex/identity.json"))
                .await
                .is_ok()
        );

        tokio::fs::set_permissions(&worktree, std::fs::Permissions::from_mode(0o755))
            .await
            .unwrap();
    }
}
