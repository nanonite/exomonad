//! Lifecycle effect handler for the `lifecycle.*` namespace.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use async_trait::async_trait;
use exomonad_proto::effects::lifecycle::*;
use serde::Deserialize;
use tokio::process::Command;
use tokio::sync::Notify;
use tracing::{info, warn};

use crate::effects::{
    dispatch_lifecycle_effect, EffectError, EffectHandler, EffectResult, LifecycleEffects,
};
use crate::handlers::agent::{
    resolve_agent_liveness, service_info_to_proto, AgentListMetadata, AgentRoutingSnapshot,
};
use crate::services::agent_control::{AgentControlService, AgentInfo};
use crate::services::{
    capture_memory, HasAgentResolver, HasGitHubClient, HasGitWorktreeService, HasInboxStore,
    HasProjectDir, HasSessionMemory, HasTeamRegistry, MemoryCapture, MemoryKind,
};

/// Handles root lifecycle effects used for idle convergence.
pub struct LifecycleHandler<C> {
    agent_control: Arc<AgentControlService<C>>,
    ctx: Arc<C>,
    shutdown_signal: Option<Arc<Notify>>,
    chainlink_bin: PathBuf,
}

impl<
        C: HasAgentResolver
            + HasGitHubClient
            + HasGitWorktreeService
            + HasInboxStore
            + HasProjectDir
            + HasSessionMemory
            + HasTeamRegistry
            + 'static,
    > LifecycleHandler<C>
{
    pub fn new(agent_control: Arc<AgentControlService<C>>, ctx: Arc<C>) -> Self {
        Self {
            agent_control,
            ctx,
            shutdown_signal: None,
            chainlink_bin: PathBuf::from("chainlink"),
        }
    }

    pub fn with_shutdown_signal(
        agent_control: Arc<AgentControlService<C>>,
        ctx: Arc<C>,
        shutdown_signal: Arc<Notify>,
    ) -> Self {
        Self {
            agent_control,
            ctx,
            shutdown_signal: Some(shutdown_signal),
            chainlink_bin: PathBuf::from("chainlink"),
        }
    }

    #[cfg(test)]
    fn with_chainlink_bin(mut self, chainlink_bin: PathBuf) -> Self {
        self.chainlink_bin = chainlink_bin;
        self
    }
}

#[async_trait]
impl<
        C: HasAgentResolver
            + HasGitHubClient
            + HasGitWorktreeService
            + HasInboxStore
            + HasProjectDir
            + HasSessionMemory
            + HasTeamRegistry
            + 'static,
    > EffectHandler for LifecycleHandler<C>
{
    fn namespace(&self) -> &str {
        "lifecycle"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        dispatch_lifecycle_effect(self, effect_type, payload, ctx).await
    }
}

#[async_trait]
impl<
        C: HasAgentResolver
            + HasGitHubClient
            + HasGitWorktreeService
            + HasInboxStore
            + HasProjectDir
            + HasSessionMemory
            + HasTeamRegistry
            + 'static,
    > LifecycleEffects for LifecycleHandler<C>
{
    async fn has_pending_work(
        &self,
        _req: HasPendingWorkEffect,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<HasPendingWorkResult> {
        let open_issue_count =
            open_chainlink_issue_count(self.ctx.project_dir(), &self.chainlink_bin).await?;
        let (total_scanned, alive_agents) = self.collect_live_non_root_agents().await?;
        info!(
            open_issue_count,
            total_scanned,
            alive_count = alive_agents.len(),
            "Evaluated lifecycle pending work"
        );
        let mut alive_agent_protos = Vec::with_capacity(alive_agents.len());
        for agent in &alive_agents {
            alive_agent_protos.push(self.agent_info_to_proto(agent).await?);
        }

        Ok(HasPendingWorkResult {
            has_pending_work: open_issue_count > 0 || !alive_agents.is_empty(),
            open_issue_count,
            alive_agent_count: alive_agents.len() as i32,
            alive_agents: alive_agent_protos,
        })
    }

    async fn shutdown_server(
        &self,
        _req: ServerShutdownEffect,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<ServerShutdownResult> {
        let (total_scanned, alive_agents) = self.collect_live_non_root_agents().await?;
        if !alive_agents.is_empty() {
            let error = live_agent_error(&alive_agents);
            warn!(
                agents = %alive_agent_names(&alive_agents),
                "Refusing server shutdown while agents are alive"
            );
            let result = ServerShutdownResult {
                success: false,
                error,
                message: String::new(),
            };
            capture_shutdown_refusal(
                ctx,
                self.ctx.as_ref(),
                total_scanned,
                alive_agents.len(),
                "agents_alive",
                &result.error,
            );
            return Ok(result);
        }

        let Some(shutdown_signal) = &self.shutdown_signal else {
            let result = ServerShutdownResult {
                success: false,
                error: "Server shutdown signal is not wired into the lifecycle handler".to_string(),
                message: String::new(),
            };
            capture_shutdown_refusal(
                ctx,
                self.ctx.as_ref(),
                total_scanned,
                0,
                "signal_unwired",
                &result.error,
            );
            return Ok(result);
        };

        shutdown_signal.notify_waiters();
        let result = ServerShutdownResult {
            success: true,
            error: String::new(),
            message: "Server shutting down".to_string(),
        };
        capture_memory(
            ctx,
            self.ctx.as_ref(),
            MemoryCapture {
                issue_id: None,
                kind: MemoryKind::SessionSummary,
                importance: 60,
                summary: format!(
                    "Server shutdown requested: state=shutting_down, scanned={total_scanned}, alive=0"
                ),
                detail: Some("state=shutting_down".to_string()),
                metadata: Some(serde_json::json!({
                    "state": "shutting_down",
                    "total_scanned": total_scanned,
                    "alive_agents": 0,
                })),
            },
        );
        Ok(result)
    }
}

#[derive(Deserialize)]
struct ChainlinkIssue {
    status: String,
}

async fn open_chainlink_issue_count(project_dir: &Path, chainlink_bin: &Path) -> EffectResult<i32> {
    let output = Command::new(chainlink_bin)
        .args(["issue", "list", "--json"])
        .current_dir(project_dir)
        .output()
        .await
        .map_err(|error| {
            EffectError::custom(
                "lifecycle_chainlink_error",
                format!("failed to run chainlink issue list --json: {error}"),
            )
        })?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(EffectError::custom(
            "lifecycle_chainlink_error",
            format!("chainlink issue list --json failed: {stderr}"),
        ));
    }

    let issues: Vec<ChainlinkIssue> = serde_json::from_slice(&output.stdout).map_err(|error| {
        EffectError::custom(
            "lifecycle_chainlink_error",
            format!("failed to parse chainlink issue list --json: {error}"),
        )
    })?;

    Ok(issues
        .into_iter()
        .filter(|issue| issue.status == "open")
        .count() as i32)
}

struct LiveAgent {
    info: AgentInfo,
    routing_snapshot: AgentRoutingSnapshot,
}

impl LiveAgent {
    fn name(&self) -> &str {
        self.info.internal_name.as_str()
    }
}

impl<
        C: HasAgentResolver
            + HasGitHubClient
            + HasGitWorktreeService
            + HasInboxStore
            + HasProjectDir
            + HasTeamRegistry
            + 'static,
    > LifecycleHandler<C>
{
    async fn collect_live_non_root_agents(&self) -> EffectResult<(usize, Vec<LiveAgent>)> {
        let infos = self.agent_control.list_agents().await.map_err(|error| {
            EffectError::custom(
                "lifecycle_agent_error",
                format!("failed to list agents for lifecycle convergence: {error}"),
            )
        })?;
        let total_scanned = infos.len();
        let mut alive_agents = Vec::new();
        for info in infos {
            if info.internal_name.as_str() == "root" {
                continue;
            }
            let (is_alive, routing_snapshot) = resolve_agent_liveness(&info).await;
            if is_alive {
                alive_agents.push(LiveAgent {
                    info,
                    routing_snapshot,
                });
            }
        }
        Ok((total_scanned, alive_agents))
    }

    async fn agent_info_to_proto(&self, agent: &LiveAgent) -> EffectResult<AgentInfoProto> {
        let agent_key = agent.info.internal_name.as_str();
        let birth_branch = self
            .ctx
            .agent_resolver()
            .get(&agent.info.internal_name)
            .await
            .map(|record| record.birth_branch.to_string())
            .unwrap_or_default();
        let has_unread = self
            .ctx
            .inbox_store()
            .has_unread(agent_key)
            .map_err(|error| {
                EffectError::custom(
                    "lifecycle_inbox_error",
                    format!("failed to read unread state for {agent_key}: {error}"),
                )
            })?;
        let last_check_inbox_at = self
            .ctx
            .inbox_store()
            .last_check_inbox_at(agent_key)
            .map_err(|error| {
                EffectError::custom(
                    "lifecycle_inbox_error",
                    format!("failed to read inbox check time for {agent_key}: {error}"),
                )
            })?
            .unwrap_or_default();

        Ok(service_info_to_proto(
            &agent.info,
            AgentListMetadata {
                birth_branch,
                has_unread,
                last_check_inbox_at,
                last_activity_at: agent.info.last_activity_at.unwrap_or_default(),
                is_alive: true,
                last_known_routing: agent.routing_snapshot.routing.clone(),
                routing_retired: agent.routing_snapshot.retired,
                routing_exit_code: agent.routing_snapshot.exit_code,
            },
        ))
    }
}

type AgentInfoProto = exomonad_proto::effects::agent::AgentInfo;

fn alive_agent_names(alive_agents: &[LiveAgent]) -> String {
    let names = alive_agents
        .iter()
        .map(LiveAgent::name)
        .collect::<Vec<_>>()
        .join(", ");
    names
}

fn live_agent_error(alive_agents: &[LiveAgent]) -> String {
    format!(
        "{} agent(s) still alive: [{}]",
        alive_agents.len(),
        alive_agent_names(alive_agents)
    )
}

fn capture_shutdown_refusal<C: HasSessionMemory>(
    ctx: &crate::effects::EffectContext,
    services: &C,
    total_scanned: usize,
    alive_agents: usize,
    reason: &str,
    error: &str,
) {
    capture_memory(
        ctx,
        services,
        MemoryCapture {
            issue_id: None,
            kind: MemoryKind::Blocker,
            importance: 80,
            summary: format!(
                "Server shutdown refused: state=shutdown_refused, reason={reason}, alive={alive_agents}"
            ),
            detail: Some(error.to_string()),
            metadata: Some(serde_json::json!({
                "state": "shutdown_refused",
                "reason": reason,
                "total_scanned": total_scanned,
                "alive_agents": alive_agents,
            })),
        },
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch, RoutingInfo, Slug};
    use crate::services::agent_control::{AgentType, Topology};
    use crate::services::agent_resolver::AgentIdentityRecord;
    use crate::services::{MemoryFilter, MemoryKind};

    fn record(name: &str) -> AgentIdentityRecord {
        AgentIdentityRecord {
            agent_name: AgentName::try_from_str(name).expect("literal is non-empty"),
            slug: Slug::try_from_str(name).expect("literal is non-empty"),
            agent_type: AgentType::OpenCode,
            birth_branch: BirthBranch::try_from_str("main.feature").expect("literal is non-empty"),
            parent_branch: BirthBranch::try_from_str("main").expect("literal is non-empty"),
            working_dir: ".exo/worktrees/feature".into(),
            display_name: "feature".to_string(),
            topology: Topology::WorktreePerAgent,
        }
    }

    #[test]
    fn root_record_is_not_pending_agent_work() {
        assert_eq!(record("root").agent_name.as_str(), "root");
        assert_ne!(record("feature-opencode").agent_name.as_str(), "root");
    }

    #[test]
    fn live_agent_error_lists_agent_names() {
        let agents = vec![
            LiveAgent {
                info: test_agent_info("leaf-a", true),
                routing_snapshot: AgentRoutingSnapshot::default(),
            },
            LiveAgent {
                info: test_agent_info("reviewer-b", true),
                routing_snapshot: AgentRoutingSnapshot::default(),
            },
        ];

        assert_eq!(
            live_agent_error(&agents),
            "2 agent(s) still alive: [leaf-a, reviewer-b]"
        );
    }

    fn test_agent_info(name: &str, has_tab: bool) -> AgentInfo {
        AgentInfo {
            internal_name: AgentName::try_from_str(name).expect("literal is non-empty"),
            has_tab,
            topology: Topology::WorktreePerAgent,
            agent_dir: None,
            slug: None,
            agent_type: Some(AgentType::OpenCode),
            pr: None,
            last_activity_at: None,
        }
    }

    fn effect_context(project_dir: &Path) -> crate::effects::EffectContext {
        crate::effects::EffectContext {
            agent_name: AgentName::try_from_str("root").expect("literal is non-empty"),
            birth_branch: BirthBranch::try_from_str("main").expect("literal is non-empty"),
            working_dir: project_dir.to_path_buf(),
        }
    }

    struct TmuxSessionGuard(String);

    impl Drop for TmuxSessionGuard {
        fn drop(&mut self) {
            let _ = std::process::Command::new("tmux")
                .args(["kill-session", "-t", &self.0])
                .status();
        }
    }

    async fn test_services(project_dir: &Path, agents: &[&str]) -> Arc<crate::services::Services> {
        let resolver = Arc::new(
            crate::services::agent_resolver::AgentResolver::load(project_dir.to_path_buf()).await,
        );
        for agent in agents {
            resolver
                .register(record(agent))
                .await
                .expect("test agent identity registers");
            if *agent != "root" {
                let worktree = project_dir.join(".exo/worktrees").join(agent);
                tokio::fs::create_dir_all(&worktree)
                    .await
                    .expect("test worktree directory created");
                tokio::fs::write(worktree.join("opencode.json"), "{}")
                    .await
                    .expect("test agent marker written");
            }
        }

        let mut services = crate::services::Services::test();
        services.project_dir = project_dir.to_path_buf();
        services.agent_resolver = resolver;
        Arc::new(services)
    }

    async fn stub_chainlink(project_dir: &Path, issues_json: &str) -> PathBuf {
        let bin = project_dir.join("chainlink");
        let script = format!(
            r#"#!/bin/sh
if [ "$1 $2 $3" = "issue list --json" ]; then
  cat <<'JSON'
{}
JSON
  exit 0
fi
echo "unexpected chainlink args: $*" >&2
exit 64
"#,
            issues_json
        );
        tokio::fs::write(&bin, script)
            .await
            .expect("stub chainlink written");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut permissions = tokio::fs::metadata(&bin)
                .await
                .expect("stub chainlink metadata")
                .permissions();
            permissions.set_mode(0o755);
            tokio::fs::set_permissions(&bin, permissions)
                .await
                .expect("stub chainlink executable");
        }
        bin
    }

    async fn handler_with_stub_chainlink(
        issues_json: &str,
        agents: &[&str],
    ) -> (
        tempfile::TempDir,
        LifecycleHandler<crate::services::Services>,
        crate::effects::EffectContext,
    ) {
        let temp_dir = tempfile::tempdir().expect("temp project dir");
        let services = test_services(temp_dir.path(), agents).await;
        let chainlink_bin = stub_chainlink(temp_dir.path(), issues_json).await;
        let ctx = effect_context(temp_dir.path());
        let agent_control = Arc::new(AgentControlService::new(services.clone()));
        let handler =
            LifecycleHandler::new(agent_control, services).with_chainlink_bin(chainlink_bin);
        (temp_dir, handler, ctx)
    }

    #[tokio::test]
    async fn has_pending_work_reports_open_chainlink_issues() {
        let (_temp_dir, handler, ctx) =
            handler_with_stub_chainlink(r#"[{"status":"open"},{"status":"closed"}]"#, &[]).await;

        let result = handler
            .has_pending_work(HasPendingWorkEffect {}, &ctx)
            .await
            .expect("handler succeeds");

        assert!(result.has_pending_work);
        assert_eq!(result.open_issue_count, 1);
        assert_eq!(result.alive_agent_count, 0);
        assert!(result.alive_agents.is_empty());
    }

    #[tokio::test]
    async fn has_pending_work_ignores_registered_agent_without_tmux_window() {
        let (_temp_dir, handler, ctx) =
            handler_with_stub_chainlink(r#"[]"#, &["root", "leaf-a"]).await;

        let result = handler
            .has_pending_work(HasPendingWorkEffect {}, &ctx)
            .await
            .expect("handler succeeds");

        assert!(!result.has_pending_work);
        assert_eq!(result.open_issue_count, 0);
        assert_eq!(result.alive_agent_count, 0);
        assert!(result.alive_agents.is_empty());
    }

    #[tokio::test]
    async fn has_pending_work_is_false_when_backlog_and_agents_are_empty() {
        let (_temp_dir, handler, ctx) = handler_with_stub_chainlink(r#"[]"#, &["root"]).await;

        let result = handler
            .has_pending_work(HasPendingWorkEffect {}, &ctx)
            .await
            .expect("handler succeeds");

        assert!(!result.has_pending_work);
        assert_eq!(result.open_issue_count, 0);
        assert_eq!(result.alive_agent_count, 0);
    }

    #[tokio::test]
    async fn shutdown_server_succeeds_with_dead_agents() {
        let temp_dir = tempfile::tempdir().expect("temp project dir");
        let services = test_services(temp_dir.path(), &["leaf-a"]).await;
        let agent_control = Arc::new(AgentControlService::new(services.clone()));
        let signal = Arc::new(Notify::new());
        let waiter = tokio::spawn({
            let signal = Arc::clone(&signal);
            async move { signal.notified().await }
        });
        tokio::task::yield_now().await;
        let handler = LifecycleHandler::with_shutdown_signal(
            agent_control,
            services.clone(),
            Arc::clone(&signal),
        );
        let ctx = effect_context(temp_dir.path());

        let result = handler
            .shutdown_server(ServerShutdownEffect {}, &ctx)
            .await
            .expect("handler succeeds");

        assert!(result.success);
        assert!(result.error.is_empty());
        tokio::time::timeout(std::time::Duration::from_millis(25), waiter)
            .await
            .expect("shutdown waiter is notified")
            .expect("shutdown waiter task succeeds");
        let records = services
            .session_memory
            .list(MemoryFilter::default())
            .expect("memory records should be readable");
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].kind, MemoryKind::SessionSummary);
        assert!(records[0]
            .metadata_json
            .as_deref()
            .is_some_and(|metadata| metadata.contains("\"state\":\"shutting_down\"")));
    }

    #[tokio::test]
    async fn has_pending_work_reports_live_tmux_agent_with_real_metadata() {
        let temp_dir = tempfile::tempdir().expect("temp project dir");
        let services = test_services(temp_dir.path(), &["root", "leaf-a"]).await;
        let session = format!("exo-lifecycle-{}", std::process::id());
        let display_name = AgentType::OpenCode.tab_display_name("leaf-a");
        let created = Command::new("tmux")
            .args(["new-session", "-d", "-s", &session, "-n", &display_name])
            .status()
            .await
            .expect("tmux is available for lifecycle regression");
        assert!(created.success(), "tmux session creation failed");
        let _tmux_guard = TmuxSessionGuard(session.clone());

        let windows = Command::new("tmux")
            .args([
                "list-windows",
                "-t",
                &session,
                "-F",
                "#{window_id}\t#{window_name}",
            ])
            .output()
            .await
            .expect("tmux window listing succeeds");
        assert!(windows.status.success(), "tmux window listing failed");
        let window_id = String::from_utf8_lossy(&windows.stdout)
            .lines()
            .filter_map(|line| {
                let (window_id, window_name) = line.split_once('\t')?;
                (window_name == display_name)
                    .then(|| crate::services::tmux_ipc::WindowId::parse(window_id).ok())
                    .flatten()
            })
            .next()
            .expect("created tmux window has a valid window id");

        let worktree = temp_dir.path().join(".exo/worktrees/leaf-a");
        let routing = RoutingInfo::window(window_id);
        routing
            .write_to_dir(&worktree)
            .await
            .expect("live agent routing written");
        let agent_dir = temp_dir.path().join(".exo/agents/leaf-a");
        tokio::fs::create_dir_all(&agent_dir)
            .await
            .expect("agent discovery directory created");
        routing
            .write_to_dir(&agent_dir)
            .await
            .expect("discovery routing written");

        let agent_control =
            Arc::new(AgentControlService::new(services.clone()).with_tmux_session(session.clone()));
        let chainlink_bin = stub_chainlink(temp_dir.path(), "[]").await;
        let signal = Arc::new(Notify::new());
        let handler = LifecycleHandler::with_shutdown_signal(
            agent_control,
            services.clone(),
            Arc::clone(&signal),
        )
        .with_chainlink_bin(chainlink_bin);
        let ctx = effect_context(temp_dir.path());

        let result = handler
            .has_pending_work(HasPendingWorkEffect {}, &ctx)
            .await
            .expect("handler succeeds");

        assert!(result.has_pending_work);
        assert_eq!(result.alive_agent_count, 1);
        assert_eq!(result.alive_agents[0].id, "leaf-a");
        assert_eq!(result.alive_agents[0].birth_branch, "main.feature");
        assert!(result.alive_agents[0].is_alive);
        assert!(!result.alive_agents[0].has_unread);

        let shutdown = handler
            .shutdown_server(ServerShutdownEffect {}, &ctx)
            .await
            .expect("shutdown handler succeeds");
        assert!(!shutdown.success);
        assert_eq!(shutdown.error, "1 agent(s) still alive: [leaf-a]");
        let records = services
            .session_memory
            .list(MemoryFilter::default())
            .expect("memory records should be readable");
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].kind, MemoryKind::Blocker);
        assert!(records[0]
            .metadata_json
            .as_deref()
            .is_some_and(|metadata| metadata.contains("\"state\":\"shutdown_refused\"")));
    }

    #[tokio::test]
    async fn shutdown_server_notifies_all_waiters_when_no_non_root_agents_are_alive() {
        let temp_dir = tempfile::tempdir().expect("temp project dir");
        let services = test_services(temp_dir.path(), &["root"]).await;
        let agent_control = Arc::new(AgentControlService::new(services.clone()));
        let signal = Arc::new(Notify::new());
        let first_waiter = tokio::spawn({
            let signal = Arc::clone(&signal);
            async move { signal.notified().await }
        });
        let second_waiter = tokio::spawn({
            let signal = Arc::clone(&signal);
            async move { signal.notified().await }
        });
        tokio::task::yield_now().await;

        let handler =
            LifecycleHandler::with_shutdown_signal(agent_control, services, Arc::clone(&signal));
        let ctx = effect_context(temp_dir.path());

        let result = handler
            .shutdown_server(ServerShutdownEffect {}, &ctx)
            .await
            .expect("handler succeeds");

        assert!(result.success);
        assert_eq!(result.message, "Server shutting down");
        tokio::time::timeout(std::time::Duration::from_millis(100), first_waiter)
            .await
            .expect("first shutdown waiter is notified")
            .expect("first shutdown waiter task succeeds");
        tokio::time::timeout(std::time::Duration::from_millis(100), second_waiter)
            .await
            .expect("second shutdown waiter is notified")
            .expect("second shutdown waiter task succeeds");
    }
}
