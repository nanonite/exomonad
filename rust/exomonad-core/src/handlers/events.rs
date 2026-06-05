//! Events effect handler for the `events.*` namespace.
//!
//! Uses proto-generated types from `exomonad_proto::effects::events`.

use crate::domain::Address;
use crate::effects::{dispatch_events_effect, EffectHandler, EffectResult, EventEffects};
use async_trait::async_trait;
use exomonad_proto::effects::events::*;
use std::sync::Arc;
use std::time::Duration;

use crate::services::{
    HasAcpRegistry, HasAgentResolver, HasEventLog, HasEventQueue, HasInboxStore, HasProjectDir,
    HasSupervisorRegistry, HasTeamRegistry,
};

fn structural_parent_session_id(
    agent_name: &crate::domain::AgentName,
    birth_branch: &crate::domain::BirthBranch,
    identity: Option<&crate::services::agent_resolver::AgentIdentityRecord>,
) -> String {
    if let Some(identity) = identity {
        if identity.topology == crate::services::agent_control::Topology::SharedDir {
            return identity.parent_branch.to_string();
        }
    }

    if agent_name.is_gemini_worker() {
        birth_branch.to_string()
    } else {
        birth_branch
            .parent()
            .map(|p| p.to_string())
            .unwrap_or_else(|| "root".to_string())
    }
}

/// Events effect handler.
///
/// Handles all effects in the `events.*` namespace.
/// Delegates to the local `EventQueue` service.
pub struct EventHandler<C> {
    ctx: Arc<C>,
    /// Event queue scope ID (server-internal UUID, NOT the birth-branch).
    event_queue_scope: String,
}

impl<C: HasEventQueue> EventHandler<C> {
    pub fn new(ctx: Arc<C>, event_queue_scope: Option<String>) -> Self {
        Self {
            ctx,
            event_queue_scope: event_queue_scope.unwrap_or_else(|| "default".to_string()),
        }
    }
}

fn message_summary(content: &str, summary: &str) -> String {
    if summary.is_empty() {
        content.chars().take(50).collect::<String>()
    } else {
        summary.to_string()
    }
}

fn explicit_message_address(
    recipient: Option<exomonad_proto::effects::events::Address>,
    effect_name: &str,
) -> EffectResult<Address> {
    let address = Address::from_proto(recipient);
    if matches!(address, Address::Supervisor) {
        return Err(crate::effects::EffectError::custom(
            "events.invalid_input",
            format!(
                "{} requires an explicit recipient (agent name or team); got empty/missing recipient",
                effect_name
            ),
        ));
    }
    Ok(address)
}

impl<C: HasSupervisorRegistry> EventHandler<C> {
    async fn lookup_supervisor(
        &self,
        agent_id: &crate::domain::AgentName,
        birth_branch: &crate::domain::BirthBranch,
    ) -> Option<crate::services::supervisor_registry::SupervisorInfo> {
        if let Some(info) = self
            .ctx
            .supervisor_registry()
            .lookup(agent_id.as_str())
            .await
        {
            return Some(info);
        }

        self.ctx
            .supervisor_registry()
            .lookup(birth_branch.as_str())
            .await
    }
}

#[async_trait]
impl<
        C: HasTeamRegistry
            + HasAcpRegistry
            + HasAgentResolver
            + HasEventLog
            + HasEventQueue
            + HasInboxStore
            + HasProjectDir
            + HasSupervisorRegistry
            + 'static,
    > EffectHandler for EventHandler<C>
{
    fn namespace(&self) -> &str {
        "events"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        dispatch_events_effect(self, effect_type, payload, ctx).await
    }
}

#[async_trait]
impl<
        C: HasTeamRegistry
            + HasAcpRegistry
            + HasAgentResolver
            + HasEventLog
            + HasEventQueue
            + HasInboxStore
            + HasProjectDir
            + HasSupervisorRegistry
            + 'static,
    > EventEffects for EventHandler<C>
{
    async fn wait_for_event(
        &self,
        req: WaitForEventRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<WaitForEventResponse> {
        tracing::info!(
            event_queue_scope = %self.event_queue_scope,
            types = ?req.types,
            timeout_secs = req.timeout_secs,
            after_event_id = req.after_event_id,
            "wait_for_event called"
        );

        // Use a default timeout of 300s if not specified or 0
        let timeout_secs = if req.timeout_secs <= 0 {
            300
        } else {
            req.timeout_secs as u64
        };

        let event = self
            .ctx
            .event_queue()
            .wait_for_event(
                &self.event_queue_scope,
                &req.types,
                Duration::from_secs(timeout_secs),
                req.after_event_id,
            )
            .await
            .map_err(|e| {
                crate::effects::EffectError::custom("events.wait_failed", e.to_string())
            })?;

        Ok(WaitForEventResponse { event: Some(event) })
    }

    async fn notify_event(
        &self,
        req: NotifyEventRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<NotifyEventResponse> {
        tracing::info!(
            session_id = %req.session_id,
            has_event = req.event.is_some(),
            "notify_event called"
        );
        // Local handling
        if let Some(event) = req.event {
            self.ctx
                .event_queue()
                .notify_event(&req.session_id, event)
                .await;
            Ok(NotifyEventResponse { success: true })
        } else {
            Ok(NotifyEventResponse { success: false })
        }
    }

    async fn notify_parent(
        &self,
        req: NotifyParentRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<NotifyParentResponse> {
        let birth_branch = &ctx.birth_branch;
        let agent_name = &ctx.agent_name;

        // Prefer agent_id from the request (set by WASM caller) over structural identity
        let (agent_id, agent_id_source) = if req.agent_id.is_empty() {
            (agent_name.clone(), "ctx")
        } else {
            (
                crate::domain::AgentName::try_from_str(req.agent_id.as_str())
                    .expect("validated string input is non-empty"),
                "request",
            )
        };

        tracing::debug!(
            agent_id = %agent_id,
            source = agent_id_source,
            "notify_parent: resolved agent_id"
        );

        // Check for override_recipient first (explicit routing)
        let override_addr = Address::from_proto(req.override_recipient.clone());

        // Resolve parent session ID:
        // 1. If override_recipient is set and not Supervisor, use that address via route_message
        // 2. Check SupervisorRegistry for explicit supervisor mapping
        // 3. Fall back to structural identity (birth-branch parent)
        if !matches!(override_addr, Address::Supervisor) {
            tracing::info!(
                address = %override_addr,
                "notify_parent: using override_recipient"
            );
            // Resolve the override address to a concrete agent key for notify_parent_delivery
            let resolver_ref = Some(self.ctx.agent_resolver());
            let (parent_session_id, tab_name) = match &override_addr {
                Address::Agent(name) => {
                    let tab =
                        crate::services::delivery::resolve_tab_name_for_agent(name, resolver_ref);
                    (name.as_str().to_string(), tab)
                }
                Address::Team {
                    member: Some(m), ..
                } => {
                    let tab =
                        crate::services::delivery::resolve_tab_name_for_agent(m, resolver_ref);
                    (m.as_str().to_string(), tab)
                }
                Address::Team { team, member: None } => {
                    let lead = self.ctx.team_registry().resolve_lead(team.as_str()).await;
                    let id = lead.unwrap_or_else(|| "root".to_string());
                    let lead_name = crate::domain::AgentName::try_from_str(id.as_str())
                        .expect("validated string input is non-empty");
                    let tab = crate::services::delivery::resolve_tab_name_for_agent(
                        &lead_name,
                        resolver_ref,
                    );
                    (id, tab)
                }
                Address::Supervisor => unreachable!(),
            };

            let status = crate::services::delivery::NotifyStatus::parse(&req.status);
            crate::services::delivery::notify_parent_delivery(
                &*self.ctx,
                &agent_id,
                &parent_session_id,
                &tab_name,
                status,
                &req.message,
                None,
                "agent",
            )
            .await;
            return Ok(NotifyParentResponse { ack: true });
        }

        // Check SupervisorRegistry by concrete agent ID first, then legacy birth-branch key.
        if let Some(info) = self.lookup_supervisor(&agent_id, birth_branch).await {
            tracing::info!(
                supervisor = %info.supervisor,
                team = %info.team,
                "notify_parent: resolved supervisor from registry"
            );
            let parent_session_id = info.supervisor.as_str();
            let supervisor_name = crate::domain::AgentName::try_from_str(parent_session_id)
                .expect("validated string input is non-empty");
            let tab_name = crate::services::delivery::resolve_tab_name_for_agent(
                &supervisor_name,
                Some(self.ctx.agent_resolver()),
            );

            let status = crate::services::delivery::NotifyStatus::parse(&req.status);
            crate::services::delivery::notify_parent_delivery(
                &*self.ctx,
                &agent_id,
                parent_session_id,
                &tab_name,
                status,
                &req.message,
                None,
                "agent",
            )
            .await;
            return Ok(NotifyParentResponse { ack: true });
        }

        // Structural fallback: worktree agents notify the parent branch;
        // shared-dir workers notify the exact parent branch recorded at spawn.
        let identity = self.ctx.agent_resolver().get(&agent_id).await;
        let parent_session_id =
            structural_parent_session_id(agent_name, birth_branch, identity.as_ref());

        tracing::info!(
            birth_branch = %birth_branch,
            parent_session_id = %parent_session_id,
            status = %req.status,
            "notify_parent: routing via structural identity"
        );

        let parent_agent = crate::domain::AgentName::try_from_str(parent_session_id.as_str())
            .expect("validated string input is non-empty");
        let tab_name = crate::services::delivery::resolve_tab_name_for_agent(
            &parent_agent,
            Some(self.ctx.agent_resolver()),
        );

        let status = crate::services::delivery::NotifyStatus::parse(&req.status);
        crate::services::delivery::notify_parent_delivery(
            &*self.ctx,
            &agent_id,
            &parent_session_id,
            &tab_name,
            status,
            &req.message,
            None,
            "agent",
        )
        .await;

        Ok(NotifyParentResponse { ack: true })
    }

    async fn send_message(
        &self,
        req: SendMessageRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SendMessageResponse> {
        let summary = message_summary(&req.content, &req.summary);
        let address = explicit_message_address(req.recipient.clone(), "send_message")?;

        tracing::info!(
            address = %address,
            sender = %ctx.agent_name,
            "send_message: routing via Address"
        );

        let outcome = crate::services::delivery::route_message(
            &*self.ctx,
            &address,
            &ctx.agent_name,
            &req.content,
            &summary,
        )
        .await;

        let method_string = outcome.method_string();
        let success = outcome.is_success();

        tracing::info!(
            otel.name = "agent.message_sent",
            address = %address,
            method = method_string,
            success = success,
            "[event] agent.message_sent"
        );

        Ok(SendMessageResponse {
            success,
            delivery_method: method_string.to_string(),
        })
    }

    async fn send_tmux_message(
        &self,
        req: SendTmuxMessageRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SendTmuxMessageResponse> {
        let summary = message_summary(&req.content, &req.summary);
        let address = explicit_message_address(req.recipient.clone(), "send_tmux_message")?;

        tracing::info!(
            address = %address,
            sender = %ctx.agent_name,
            "send_tmux_message: routing via tmux stdin"
        );

        let outcome = crate::services::delivery::route_tmux_message(
            &*self.ctx,
            &address,
            &ctx.agent_name,
            &req.content,
            &summary,
        )
        .await;
        let method_string = outcome.method_string();
        let success = outcome.is_success();

        Ok(SendTmuxMessageResponse {
            success,
            delivery_method: method_string.to_string(),
        })
    }

    async fn send_mailbox_message(
        &self,
        req: SendMailboxMessageRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SendMailboxMessageResponse> {
        if !crate::services::delivery::mailbox_protocol_available() {
            return Err(crate::effects::EffectError::custom(
                "events.mailbox_unavailable",
                crate::services::delivery::MAILBOX_PROTOCOL_UNAVAILABLE_MESSAGE.to_string(),
            ));
        }

        let summary = message_summary(&req.content, &req.summary);
        let address = explicit_message_address(req.recipient.clone(), "send_mailbox_message")?;

        tracing::info!(
            address = %address,
            sender = %ctx.agent_name,
            "send_mailbox_message: routing via Teams inbox"
        );

        let outcome = crate::services::delivery::route_mailbox_message(
            &*self.ctx,
            &address,
            &ctx.agent_name,
            &req.content,
            &summary,
        )
        .await;
        let method_string = outcome.method_string();
        let success = outcome.is_success();

        Ok(SendMailboxMessageResponse {
            success,
            delivery_method: method_string.to_string(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch, Slug};
    use crate::services::agent_control::{AgentType, Topology};
    use crate::services::agent_resolver::AgentIdentityRecord;
    use std::path::PathBuf;

    #[test]
    fn test_event_handler_namespace() {
        let services = Arc::new(crate::services::Services::test());
        let handler = EventHandler::new(services, None);
        assert_eq!(handler.namespace(), "events");
    }

    #[test]
    fn shared_dir_worker_notifies_recorded_parent_branch() {
        let agent_name = AgentName::try_from_str("chainlink-codex-worker-codex")
            .expect("literal validated string is non-empty");
        let birth_branch = BirthBranch::try_from_str("main.chainlink-codex-tl-codex")
            .expect("literal validated string is non-empty");
        let identity = AgentIdentityRecord {
            agent_name: agent_name.clone(),
            slug: Slug::try_from_str("chainlink-codex-worker")
                .expect("literal validated string is non-empty"),
            agent_type: AgentType::Codex,
            birth_branch: birth_branch.clone(),
            parent_branch: BirthBranch::try_from_str("main.chainlink-codex-tl-codex")
                .expect("literal validated string is non-empty"),
            working_dir: PathBuf::from(".exo/worktrees/chainlink-codex-tl-codex"),
            display_name: "🤖 chainlink-codex-worker-codex".to_string(),
            topology: Topology::SharedDir,
        };

        assert_eq!(
            structural_parent_session_id(&agent_name, &birth_branch, Some(&identity)),
            "main.chainlink-codex-tl-codex"
        );
    }

    #[test]
    fn worktree_agent_notifies_birth_branch_parent() {
        let agent_name = AgentName::try_from_str("codex-leaf-codex")
            .expect("literal validated string is non-empty");
        let birth_branch = BirthBranch::try_from_str("main.codex-tl-codex.codex-leaf-codex")
            .expect("literal validated string is non-empty");

        assert_eq!(
            structural_parent_session_id(&agent_name, &birth_branch, None),
            "main.codex-tl-codex"
        );
    }
}
