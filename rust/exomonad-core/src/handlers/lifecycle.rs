//! Lifecycle effect handler for the `lifecycle.*` namespace.

use std::sync::Arc;

use async_trait::async_trait;
use exomonad_proto::effects::lifecycle::*;
use tokio::sync::Notify;
use tracing::warn;

use crate::effects::{
    dispatch_lifecycle_effect, EffectError, EffectHandler, EffectResult, LifecycleEffects,
};
use crate::handlers::agent::resolve_agent_liveness;
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
        }
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

struct LiveAgent {
    info: AgentInfo,
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
            let (is_alive, _routing_snapshot) = resolve_agent_liveness(&info).await;
            if is_alive {
                alive_agents.push(LiveAgent { info });
            }
        }
        Ok((total_scanned, alive_agents))
    }
}

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
    use crate::domain::{AgentName, BirthBranch, Slug};
    use crate::services::agent_control::{AgentType, Topology};
    use crate::services::agent_resolver::AgentIdentityRecord;
    use crate::services::{MemoryFilter, MemoryKind};
    use std::path::Path;

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
            model: None,
            effort: None,
            ledger_owned: false,
            slice_id: None,
        }
    }

    #[test]
    fn root_record_is_not_counted_as_live_agent() {
        assert_eq!(record("root").agent_name.as_str(), "root");
        assert_ne!(record("feature-opencode").agent_name.as_str(), "root");
    }

    #[test]
    fn live_agent_error_lists_agent_names() {
        let agents = vec![
            LiveAgent {
                info: test_agent_info("leaf-a", true),
            },
            LiveAgent {
                info: test_agent_info("reviewer-b", true),
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
            worktree_path: None,
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
