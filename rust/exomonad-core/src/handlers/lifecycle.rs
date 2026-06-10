//! Lifecycle effect handler for the `lifecycle.*` namespace.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use async_trait::async_trait;
use exomonad_proto::effects::agent::{AgentInfo, AgentType as ProtoAgentType};
use exomonad_proto::effects::lifecycle::*;
use serde::Deserialize;
use tokio::process::Command;
use tokio::sync::Notify;

use crate::effects::{
    dispatch_lifecycle_effect, EffectError, EffectHandler, EffectResult, LifecycleEffects,
};
use crate::services::agent_control::AgentType;
use crate::services::agent_resolver::AgentIdentityRecord;
use crate::services::{HasAgentResolver, HasProjectDir};

/// Handles root lifecycle effects used for idle convergence.
pub struct LifecycleHandler<C> {
    ctx: Arc<C>,
    shutdown_signal: Option<Arc<Notify>>,
    chainlink_bin: PathBuf,
}

impl<C: HasAgentResolver + HasProjectDir + 'static> LifecycleHandler<C> {
    pub fn new(ctx: Arc<C>) -> Self {
        Self {
            ctx,
            shutdown_signal: None,
            chainlink_bin: PathBuf::from("chainlink"),
        }
    }

    pub fn with_shutdown_signal(ctx: Arc<C>, shutdown_signal: Arc<Notify>) -> Self {
        Self {
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
impl<C: HasAgentResolver + HasProjectDir + 'static> EffectHandler for LifecycleHandler<C> {
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
impl<C: HasAgentResolver + HasProjectDir + 'static> LifecycleEffects for LifecycleHandler<C> {
    async fn has_pending_work(
        &self,
        _req: HasPendingWorkEffect,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<HasPendingWorkResult> {
        let open_issue_count =
            open_chainlink_issue_count(self.ctx.project_dir(), &self.chainlink_bin).await?;
        let alive_agents = live_non_root_agents(self.ctx.as_ref()).await;

        Ok(HasPendingWorkResult {
            has_pending_work: open_issue_count > 0 || !alive_agents.is_empty(),
            open_issue_count,
            alive_agent_count: alive_agents.len() as i32,
            alive_agents: alive_agents
                .into_iter()
                .map(agent_record_to_proto)
                .collect(),
        })
    }

    async fn shutdown_server(
        &self,
        _req: ServerShutdownEffect,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<ServerShutdownResult> {
        let alive_agents = live_non_root_agents(self.ctx.as_ref()).await;
        if !alive_agents.is_empty() {
            let error = live_agent_error(&alive_agents);
            return Ok(ServerShutdownResult {
                success: false,
                error,
                message: String::new(),
            });
        }

        let Some(shutdown_signal) = &self.shutdown_signal else {
            return Ok(ServerShutdownResult {
                success: false,
                error: "Server shutdown signal is not wired into the lifecycle handler".to_string(),
                message: String::new(),
            });
        };

        shutdown_signal.notify_one();
        Ok(ServerShutdownResult {
            success: true,
            error: String::new(),
            message: "Server shutting down".to_string(),
        })
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

async fn live_non_root_agents<C: HasAgentResolver>(ctx: &C) -> Vec<AgentIdentityRecord> {
    ctx.agent_resolver()
        .all()
        .await
        .into_iter()
        .filter(is_non_root_agent)
        .collect()
}

fn is_non_root_agent(record: &AgentIdentityRecord) -> bool {
    record.agent_name.as_str() != "root"
}

fn live_agent_error(alive_agents: &[AgentIdentityRecord]) -> String {
    let names = alive_agents
        .iter()
        .map(|agent| agent.agent_name.to_string())
        .collect::<Vec<_>>()
        .join(", ");
    format!("{} agent(s) still alive: [{}]", alive_agents.len(), names)
}

fn agent_record_to_proto(record: AgentIdentityRecord) -> AgentInfo {
    AgentInfo {
        id: record.agent_name.to_string(),
        issue: String::new(),
        worktree_path: record.working_dir.display().to_string(),
        branch_name: String::new(),
        agent_type: service_agent_type_to_proto(record.agent_type),
        role: 0,
        alive: true,
        mux_window: record.display_name,
        error: String::new(),
        pr_number: 0,
        pr_url: String::new(),
        topology: record.topology.to_proto(),
        pane_id: String::new(),
        birth_branch: record.birth_branch.to_string(),
        has_unread: false,
        last_check_inbox_at: 0,
        is_alive: true,
        ..Default::default()
    }
}

fn service_agent_type_to_proto(agent_type: AgentType) -> i32 {
    match agent_type {
        AgentType::Claude => ProtoAgentType::Claude as i32,
        AgentType::Gemini => ProtoAgentType::Gemini as i32,
        AgentType::Shoal => ProtoAgentType::Shoal as i32,
        AgentType::OpenCode => ProtoAgentType::Opencode as i32,
        AgentType::Codex => ProtoAgentType::Codex as i32,
        AgentType::Process => ProtoAgentType::Unspecified as i32,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch, Slug};
    use crate::services::agent_control::Topology;

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
        assert!(!is_non_root_agent(&record("root")));
        assert!(is_non_root_agent(&record("feature-opencode")));
    }

    #[test]
    fn live_agent_error_lists_agent_names() {
        let agents = vec![record("leaf-a"), record("reviewer-b")];

        assert_eq!(
            live_agent_error(&agents),
            "2 agent(s) still alive: [leaf-a, reviewer-b]"
        );
    }

    #[test]
    fn agent_record_maps_to_lifecycle_proto_agent_info() {
        let proto = agent_record_to_proto(record("leaf-a"));

        assert_eq!(proto.id, "leaf-a");
        assert_eq!(proto.agent_type, ProtoAgentType::Opencode as i32);
        assert_eq!(proto.topology, Topology::WorktreePerAgent.to_proto());
        assert!(proto.alive);
        assert!(proto.is_alive);
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
        let handler = LifecycleHandler::new(services).with_chainlink_bin(chainlink_bin);
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
    async fn has_pending_work_reports_alive_non_root_agents() {
        let (_temp_dir, handler, ctx) =
            handler_with_stub_chainlink(r#"[]"#, &["root", "leaf-a"]).await;

        let result = handler
            .has_pending_work(HasPendingWorkEffect {}, &ctx)
            .await
            .expect("handler succeeds");

        assert!(result.has_pending_work);
        assert_eq!(result.open_issue_count, 0);
        assert_eq!(result.alive_agent_count, 1);
        assert_eq!(result.alive_agents[0].id, "leaf-a");
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
    async fn shutdown_server_rejects_alive_agents_without_notifying() {
        let temp_dir = tempfile::tempdir().expect("temp project dir");
        let services = test_services(temp_dir.path(), &["leaf-a"]).await;
        let signal = Arc::new(Notify::new());
        let handler = LifecycleHandler::with_shutdown_signal(services, Arc::clone(&signal));
        let ctx = effect_context(temp_dir.path());

        let result = handler
            .shutdown_server(ServerShutdownEffect {}, &ctx)
            .await
            .expect("handler succeeds");

        assert!(!result.success);
        assert_eq!(result.error, "1 agent(s) still alive: [leaf-a]");
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(25), signal.notified())
                .await
                .is_err()
        );
    }

    #[tokio::test]
    async fn shutdown_server_notifies_when_no_non_root_agents_are_alive() {
        let temp_dir = tempfile::tempdir().expect("temp project dir");
        let services = test_services(temp_dir.path(), &["root"]).await;
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

        let handler = LifecycleHandler::with_shutdown_signal(services, Arc::clone(&signal));
        let ctx = effect_context(temp_dir.path());

        let result = handler
            .shutdown_server(ServerShutdownEffect {}, &ctx)
            .await
            .expect("handler succeeds");

        assert!(result.success);
        assert_eq!(result.message, "Server shutting down");
        let first_done = tokio::time::timeout(std::time::Duration::from_millis(100), first_waiter)
            .await
            .is_ok();
        let second_done = tokio::time::timeout(std::time::Duration::from_millis(25), second_waiter)
            .await
            .is_ok();
        assert_ne!(
            first_done, second_done,
            "exactly one shutdown waiter should be notified"
        );
    }
}
