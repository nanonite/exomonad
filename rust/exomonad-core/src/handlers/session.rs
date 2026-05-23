//! Session effect handler for the `session.*` namespace.
//!
//! Stores Claude Code session UUIDs so spawn_subtree can use --resume --fork-session.
//! Stores Claude Teams info so notify_parent can route via Teams inbox.

use crate::domain::{ClaudeSessionUuid, RoutingInfo};
use crate::effects::{dispatch_session_effect, EffectResult, ResultExt, SessionEffects};
use crate::services::agent_resolver::AgentIdentityRecord;
use crate::services::supervisor_registry::SupervisorInfo;
use crate::services::tmux_ipc::TmuxIpc;
use async_trait::async_trait;
use claude_teams_bridge::TeamInfo;
use exomonad_proto::effects::session::*;
use std::path::Path;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tracing::info;

use crate::services::{
    HasClaudeSessionRegistry, HasProjectDir, HasSupervisorRegistry, HasTeamRegistry,
};

/// Session effect handler.
pub struct SessionHandler<C> {
    ctx: Arc<C>,
}

impl<
        C: HasClaudeSessionRegistry
            + HasTeamRegistry
            + HasSupervisorRegistry
            + HasProjectDir
            + 'static,
    > SessionHandler<C>
{
    pub fn new(ctx: Arc<C>) -> Self {
        Self { ctx }
    }
}

#[async_trait]
impl<
        C: HasClaudeSessionRegistry
            + HasTeamRegistry
            + HasSupervisorRegistry
            + HasProjectDir
            + 'static,
    > crate::effects::EffectHandler for SessionHandler<C>
{
    fn namespace(&self) -> &str {
        "session"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        ctx: &crate::effects::EffectContext,
    ) -> crate::effects::EffectResult<Vec<u8>> {
        dispatch_session_effect(self, effect_type, payload, ctx).await
    }
}

#[async_trait]
impl<
        C: HasClaudeSessionRegistry
            + HasTeamRegistry
            + HasSupervisorRegistry
            + HasProjectDir
            + 'static,
    > SessionEffects for SessionHandler<C>
{
    async fn register_claude_id(
        &self,
        req: RegisterClaudeSessionRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<RegisterClaudeSessionResponse> {
        let agent_name = &ctx.agent_name;
        let key = if agent_name.as_str().is_empty() {
            "root".to_string()
        } else {
            agent_name.to_string()
        };

        let claude_uuid = ClaudeSessionUuid::try_from(req.claude_session_id.clone())
            .map_err(|e| crate::effects::EffectError::invalid_input(e.to_string()))?;

        info!(
            key = %key,
            claude_session_id = %claude_uuid,
            "Registering Claude session via effect"
        );

        self.ctx
            .claude_session_registry()
            .register(&key, claude_uuid.clone())
            .await;

        // Also store under slug variant (strip -claude suffix) for broader lookup
        if let Some(slug) = key.strip_suffix("-claude") {
            self.ctx
                .claude_session_registry()
                .register(slug, claude_uuid)
                .await;
        }

        Ok(RegisterClaudeSessionResponse { success: true })
    }

    async fn register_team(
        &self,
        req: RegisterTeamRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<RegisterTeamResponse> {
        let agent_name = &ctx.agent_name;
        let key = if agent_name.as_str().is_empty() {
            "root".to_string()
        } else {
            agent_name.to_string()
        };

        info!(
            key = %key,
            team_name = %req.team_name,
            inbox_name = %req.inbox_name,
            "Registering Claude Teams info via effect"
        );

        // Register in-memory only — Claude Code owns team directory lifecycle via TeamCreate.
        // SessionStart hook instructs Claude to call TeamCreate, which creates ~/.claude/teams/{name}/.
        let team_info = TeamInfo {
            team_name: req.team_name.clone(),
            inbox_name: req.inbox_name.clone(),
        };

        self.ctx
            .team_registry()
            .register(&key, team_info.clone())
            .await;

        // Also store under birth_branch — notify_parent looks up by parent's birth_branch.
        let bb = ctx.birth_branch.to_string();
        if bb != key {
            info!(key = %bb, team_name = %req.team_name, "Also registering team under birth_branch");
            self.ctx
                .team_registry()
                .register(&bb, team_info.clone())
                .await;
        }

        // Also store under slug variant for broader lookup
        if let Some(slug) = key.strip_suffix("-claude") {
            self.ctx.team_registry().register(slug, team_info).await;
        }

        Ok(RegisterTeamResponse { success: true })
    }

    async fn register_supervisor(
        &self,
        req: RegisterSupervisorRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<RegisterSupervisorResponse> {
        let children: Vec<String> = req.children.into_iter().collect();
        let count = children.len() as i32;

        if req.supervisor.is_empty() || req.team.is_empty() {
            return Err(crate::effects::EffectError::invalid_input(
                "supervisor and team must be non-empty".to_string(),
            ));
        }

        let supervisor_name = crate::domain::AgentName::try_from(req.supervisor.clone())
            .map_err(|e| crate::effects::EffectError::invalid_input(e.to_string()))?;
        let team_name = crate::domain::TeamName::try_from(req.team.clone())
            .map_err(|e| crate::effects::EffectError::invalid_input(e.to_string()))?;

        info!(
            supervisor = %req.supervisor,
            team = %req.team,
            children_count = count,
            "Registering supervisor for children"
        );

        self.ctx
            .supervisor_registry()
            .register(
                &children,
                SupervisorInfo {
                    supervisor: supervisor_name,
                    team: team_name,
                },
            )
            .await;

        Ok(RegisterSupervisorResponse {
            success: true,
            registered_count: count,
        })
    }

    async fn deregister_supervisor(
        &self,
        req: DeregisterSupervisorRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<DeregisterSupervisorResponse> {
        let children: Vec<String> = req.children.into_iter().collect();
        info!(
            children_count = children.len(),
            "Deregistering supervisor for children"
        );
        self.ctx.supervisor_registry().deregister(&children).await;
        Ok(DeregisterSupervisorResponse { success: true })
    }

    async fn list_agents(
        &self,
        req: ListAgentsRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<ListAgentsResponse> {
        let tmux = std::env::var("EXOMONAD_TMUX_SESSION")
            .ok()
            .filter(|session| !session.trim().is_empty())
            .map(|session| TmuxIpc::new(&session));
        let agents = list_agent_statuses(self.ctx.project_dir(), tmux.as_ref(), req.include_dead)
            .await
            .effect_err("session")?;
        Ok(ListAgentsResponse { agents })
    }

    async fn deregister_team(
        &self,
        _req: DeregisterTeamRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<DeregisterTeamResponse> {
        let agent_name = &ctx.agent_name;
        let key = if agent_name.as_str().is_empty() {
            "root".to_string()
        } else {
            agent_name.to_string()
        };

        info!(key = %key, "Deregistering Claude Teams info via effect");

        // Remove all exomonad synthetic members from config.json BEFORE
        // deregistering from TeamRegistry. This prevents ghost members
        // from blocking CC's TeamDelete (which checks config.json).
        if let Some(team_info) = self.ctx.team_registry().get(&key).await {
            let team_name = crate::domain::TeamName::try_from_str(team_info.team_name.as_str())
                .expect("validated string input is non-empty");
            match crate::services::synthetic_members::remove_all_synthetic_members(&team_name) {
                Ok(removed) => {
                    info!(team = %team_name, removed, "Cleaned synthetic members before team deregister");
                }
                Err(e) => {
                    tracing::warn!(team = %team_name, error = %e, "Failed to clean synthetic members");
                }
            }
        }

        self.ctx.team_registry().deregister(&key).await;

        // Also deregister under birth_branch
        let bb = ctx.birth_branch.to_string();
        if bb != key {
            self.ctx.team_registry().deregister(&bb).await;
        }

        // Also deregister slug variant
        if let Some(slug) = key.strip_suffix("-claude") {
            self.ctx.team_registry().deregister(slug).await;
        }

        Ok(DeregisterTeamResponse { success: true })
    }
}

async fn list_agent_statuses(
    project_dir: &Path,
    tmux: Option<&TmuxIpc>,
    include_dead: bool,
) -> anyhow::Result<Vec<AgentStatus>> {
    let agents_dir = project_dir.join(".exo/agents");
    let mut entries = match tokio::fs::read_dir(&agents_dir).await {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(error) => return Err(error.into()),
    };

    let mut agents = Vec::new();
    while let Some(entry) = entries.next_entry().await? {
        if !entry.file_type().await?.is_dir() {
            continue;
        }
        let name = entry.file_name().to_string_lossy().to_string();
        let agent_dir = entry.path();
        let status = agent_status_from_dir(&name, &agent_dir, tmux).await;
        if include_dead || status.window_alive {
            agents.push(status);
        }
    }
    agents.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(agents)
}

async fn agent_status_from_dir(
    name: &str,
    agent_dir: &Path,
    tmux: Option<&TmuxIpc>,
) -> AgentStatus {
    let routing = RoutingInfo::read_from_dir(agent_dir).await.ok();
    let identity = read_identity(agent_dir).await;
    let birth_branch = match identity.as_ref() {
        Some(record) => record.birth_branch.to_string(),
        None => read_trimmed(agent_dir.join(".birth_branch"))
            .await
            .unwrap_or_default(),
    };
    let issue = read_trimmed(agent_dir.join("active_issue"))
        .await
        .unwrap_or_default();
    let spawned_at = read_trimmed(agent_dir.join("spawned_at"))
        .await
        .and_then(|value| value.parse::<u64>().ok());
    let age_mins = spawned_at.map(age_mins_since).unwrap_or(0);
    let window_id = routing
        .as_ref()
        .and_then(|routing| routing.window_id.as_ref())
        .map(ToString::to_string)
        .unwrap_or_default();
    let pane_id = routing
        .as_ref()
        .and_then(|routing| routing.pane_id.as_ref())
        .map(ToString::to_string)
        .unwrap_or_default();
    let window_alive = routing_alive(routing.as_ref(), tmux).await;
    let role = infer_role(name, routing.as_ref(), &birth_branch);

    AgentStatus {
        name: name.to_string(),
        role,
        issue,
        window_id,
        pane_id,
        window_alive,
        age_mins,
        birth_branch,
    }
}

async fn routing_alive(routing: Option<&RoutingInfo>, tmux: Option<&TmuxIpc>) -> bool {
    let Some(tmux) = tmux else {
        return false;
    };
    let Some(routing) = routing else {
        return false;
    };
    if let Some(window_id) = &routing.window_id {
        return tmux.window_exists(window_id).await.unwrap_or(false);
    }
    if let Some(pane_id) = &routing.pane_id {
        return tmux.pane_exists(pane_id).await.unwrap_or(false);
    }
    false
}

async fn read_identity(agent_dir: &Path) -> Option<AgentIdentityRecord> {
    let content = tokio::fs::read_to_string(agent_dir.join("identity.json"))
        .await
        .ok()?;
    serde_json::from_str(&content).ok()
}

async fn read_trimmed(path: impl AsRef<Path>) -> Option<String> {
    tokio::fs::read_to_string(path)
        .await
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn age_mins_since(spawned_at: u64) -> u64 {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    now.saturating_sub(spawned_at) / 60
}

fn infer_role(name: &str, routing: Option<&RoutingInfo>, birth_branch: &str) -> String {
    if name.starts_with("review-pr-") || birth_branch.starts_with("review-pr-") {
        "reviewer".to_string()
    } else if routing
        .and_then(|routing| routing.pane_id.as_ref())
        .is_some()
    {
        "worker".to_string()
    } else if name.contains("-tl-")
        || birth_branch
            .rsplit('.')
            .next()
            .is_some_and(|leaf| leaf.contains("-tl-"))
    {
        "tl".to_string()
    } else {
        "dev".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch};
    use crate::effects::{EffectContext, EffectHandler};
    use crate::services::Services;

    fn test_ctx() -> EffectContext {
        EffectContext {
            agent_name: AgentName::try_from_str("test")
                .expect("literal validated string is non-empty"),
            birth_branch: BirthBranch::try_from_str("main")
                .expect("literal validated string is non-empty"),
            working_dir: std::path::PathBuf::from("."),
        }
    }

    #[test]
    fn test_namespace() {
        let services = Arc::new(Services::test());
        let handler = SessionHandler::new(services);
        assert_eq!(handler.namespace(), "session");
    }

    #[tokio::test]
    async fn test_register_claude_id() {
        let services = Arc::new(Services::test());
        let handler = SessionHandler::new(services.clone());
        let ctx = test_ctx();

        let req = RegisterClaudeSessionRequest {
            claude_session_id: "7343ced0-1d95-450a-8ae5-976fe94421f0".into(),
        };

        let resp = handler.register_claude_id(req, &ctx).await.unwrap();
        assert!(resp.success);

        let registered = services.claude_session_registry.get("test").await.unwrap();
        assert_eq!(
            registered.to_string(),
            "7343ced0-1d95-450a-8ae5-976fe94421f0"
        );
    }

    #[tokio::test]
    async fn test_register_team() {
        let services = Arc::new(Services::test());
        let handler = SessionHandler::new(services.clone());
        let ctx = test_ctx();

        let req = RegisterTeamRequest {
            team_name: "test-team".into(),
            inbox_name: "test-inbox".into(),
        };

        let resp = handler.register_team(req, &ctx).await.unwrap();
        assert!(resp.success);

        let info = services.team_registry.get("test").await.unwrap();
        assert_eq!(info.team_name, "test-team");
        assert_eq!(info.inbox_name, "test-inbox");

        let info_bb = services.team_registry.get("main").await.unwrap();
        assert_eq!(info_bb.team_name, "test-team");
    }

    #[tokio::test]
    async fn test_deregister_team() {
        let services = Arc::new(Services::test());
        let handler = SessionHandler::new(services.clone());
        let ctx = test_ctx();

        handler
            .register_team(
                RegisterTeamRequest {
                    team_name: "test-team".into(),
                    inbox_name: "test-inbox".into(),
                },
                &ctx,
            )
            .await
            .unwrap();

        assert!(services.team_registry.get("test").await.is_some());
        assert!(services.team_registry.get("main").await.is_some());

        let resp = handler
            .deregister_team(DeregisterTeamRequest {}, &ctx)
            .await
            .unwrap();
        assert!(resp.success);

        assert!(services.team_registry.get("test").await.is_none());
        assert!(services.team_registry.get("main").await.is_none());
    }

    #[tokio::test]
    async fn test_register_team_slug_variant() {
        let services = Arc::new(Services::test());
        let handler = SessionHandler::new(services.clone());

        let ctx = EffectContext {
            agent_name: AgentName::try_from_str("foo-claude")
                .expect("literal validated string is non-empty"),
            birth_branch: BirthBranch::try_from_str("main")
                .expect("literal validated string is non-empty"),
            working_dir: std::path::PathBuf::from("."),
        };

        let req = RegisterTeamRequest {
            team_name: "test-team".into(),
            inbox_name: "test-inbox".into(),
        };

        handler.register_team(req, &ctx).await.unwrap();

        assert!(services.team_registry.get("foo-claude").await.is_some());
        assert!(services.team_registry.get("foo").await.is_some());
    }

    #[tokio::test]
    async fn test_register_claude_id_slug_variant() {
        let services = Arc::new(Services::test());
        let handler = SessionHandler::new(services.clone());

        let ctx = EffectContext {
            agent_name: AgentName::try_from_str("foo-claude")
                .expect("literal validated string is non-empty"),
            birth_branch: BirthBranch::try_from_str("main")
                .expect("literal validated string is non-empty"),
            working_dir: std::path::PathBuf::from("."),
        };

        let req = RegisterClaudeSessionRequest {
            claude_session_id: "7343ced0-1d95-450a-8ae5-976fe94421f0".into(),
        };

        handler.register_claude_id(req, &ctx).await.unwrap();

        assert!(services
            .claude_session_registry
            .get("foo-claude")
            .await
            .is_some());
        assert!(services.claude_session_registry.get("foo").await.is_some());
    }
}
