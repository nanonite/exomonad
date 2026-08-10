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
    capture_memory, HasAgentResolver, HasEventLog, HasEventQueue, HasInboxStore, HasProjectDir,
    HasSessionMemory, HasSupervisorRegistry, HasTeamRegistry, MemoryCapture, MemoryKind,
};

fn structural_parent_session_id(
    _agent_name: &crate::domain::AgentName,
    birth_branch: &crate::domain::BirthBranch,
    identity: Option<&crate::services::agent_resolver::AgentIdentityRecord>,
) -> String {
    let parent_branch = match identity {
        Some(identity)
            if identity.topology == crate::services::agent_control::Topology::SharedDir =>
        {
            identity.parent_branch.to_string()
        }
        _ => birth_branch
            .parent()
            .map(|p| p.to_string())
            .unwrap_or_else(|| "root".to_string()),
    };

    crate::services::delivery::canonical_parent_recipient(&parent_branch)
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
    if is_parent_alias(&address) {
        return Err(crate::effects::EffectError::custom(
            "events.invalid_input",
            format!(
                "{} cannot route to reserved agent alias 'parent'; use notify_parent without an override",
                effect_name
            ),
        ));
    }
    Ok(address)
}

fn is_parent_alias(address: &Address) -> bool {
    matches!(address, Address::Agent(name) if name.as_str() == "parent")
}

fn silent_noop_handoff(message: &str) -> bool {
    let lower = message.to_ascii_lowercase();
    [
        "no commit",
        "no pr",
        "no pull request",
        "no-op",
        "no changes",
        "could not proceed",
    ]
    .iter()
    .any(|marker| lower.contains(marker))
}

fn bounded_handoff_reason(message: &str) -> String {
    message
        .lines()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("")
        .trim()
        .chars()
        .take(160)
        .collect()
}

fn capture_parent_notification<C: HasSessionMemory + HasEventLog>(
    ctx: &crate::effects::EffectContext,
    services: &C,
    agent_id: &crate::domain::AgentName,
    parent_session_id: &str,
    status: crate::services::delivery::NotifyStatus,
    message: &str,
    delivery_result: crate::services::delivery::DeliveryResult,
) {
    let kind = match status {
        crate::services::delivery::NotifyStatus::Success => MemoryKind::ChildHandoff,
        crate::services::delivery::NotifyStatus::Failure
        | crate::services::delivery::NotifyStatus::Stuck => MemoryKind::Blocker,
    };
    let summary = match kind {
        MemoryKind::ChildHandoff => format!("Child handoff reported to parent {parent_session_id}"),
        MemoryKind::Blocker => format!("Child blocker reported to parent {parent_session_id}"),
        _ => unreachable!("notification capture only uses handoff or blocker kinds"),
    };

    if matches!(
        status,
        crate::services::delivery::NotifyStatus::Failure
            | crate::services::delivery::NotifyStatus::Stuck
    ) && silent_noop_handoff(message)
    {
        if let Some(log) = services.event_log() {
            let _ = log.append(
                "agent.stuck",
                agent_id.as_ref(),
                &serde_json::json!({
                    "kind": "silent_noop_handoff",
                    "parent": parent_session_id,
                    "status": status.as_str(),
                    "reason": bounded_handoff_reason(message),
                    "guidance_required": true,
                    "retry_policy": "same_harness_or_resume_pr",
                }),
            );
        }
    }

    capture_memory(
        ctx,
        services,
        MemoryCapture {
            issue_id: None,
            kind,
            importance: if kind == MemoryKind::Blocker { 80 } else { 60 },
            summary,
            detail: Some(message.to_string()),
            metadata: Some(serde_json::json!({
                "parent": parent_session_id,
                "status": status.as_str(),
                "delivery": format!("{delivery_result:?}"),
                "agent": agent_id.as_str(),
            })),
        },
    );
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
            + HasAgentResolver
            + HasEventLog
            + HasEventQueue
            + HasInboxStore
            + HasProjectDir
            + HasSessionMemory
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
            + HasAgentResolver
            + HasEventLog
            + HasEventQueue
            + HasInboxStore
            + HasProjectDir
            + HasSessionMemory
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
        let mut override_addr = Address::from_proto(req.override_recipient.clone());
        if is_parent_alias(&override_addr) {
            tracing::warn!(
                agent_id = %agent_id,
                "notify_parent: resolving reserved override_recipient alias 'parent' via normal parent routing"
            );
            override_addr = Address::Supervisor;
        }

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
            let delivery_result = crate::services::delivery::notify_parent_delivery(
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
            capture_parent_notification(
                ctx,
                self.ctx.as_ref(),
                &agent_id,
                &parent_session_id,
                status,
                &req.message,
                delivery_result,
            );
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
            let delivery_result = crate::services::delivery::notify_parent_delivery(
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
            capture_parent_notification(
                ctx,
                self.ctx.as_ref(),
                &agent_id,
                parent_session_id,
                status,
                &req.message,
                delivery_result,
            );
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
        let delivery_result = crate::services::delivery::notify_parent_delivery(
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
        capture_parent_notification(
            ctx,
            self.ctx.as_ref(),
            &agent_id,
            &parent_session_id,
            status,
            &req.message,
            delivery_result,
        );

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
    use crate::services::{MemoryFilter, SessionMemoryService};
    use std::path::PathBuf;

    #[test]
    fn test_event_handler_namespace() {
        let services = Arc::new(crate::services::Services::test());
        let handler = EventHandler::new(services, None);
        assert_eq!(handler.namespace(), "events");
    }

    fn test_ctx(agent_name: &str, birth_branch: &str) -> crate::effects::EffectContext {
        crate::effects::EffectContext {
            agent_name: AgentName::try_from_str(agent_name)
                .expect("literal validated string is non-empty"),
            birth_branch: BirthBranch::try_from_str(birth_branch)
                .expect("literal validated string is non-empty"),
            working_dir: PathBuf::from("."),
        }
    }

    fn proto_agent_address(agent: &str) -> Option<exomonad_proto::effects::events::Address> {
        use exomonad_proto::effects::events::{address::Kind, Address as ProtoAddress};

        Some(ProtoAddress {
            kind: Some(Kind::Agent(agent.to_string())),
        })
    }

    struct RuntimeParentCase {
        runtime: AgentType,
        agent_name: &'static str,
        slug: &'static str,
        birth_branch: &'static str,
        parent_branch: &'static str,
        topology: Topology,
        expected_parent: &'static str,
    }

    async fn services_with_identity(case: &RuntimeParentCase) -> Arc<crate::services::Services> {
        let temp_dir = tempfile::tempdir().expect("tempdir should be created");
        let resolver = crate::services::AgentResolver::load(temp_dir.path().to_path_buf()).await;
        resolver
            .register(AgentIdentityRecord {
                agent_name: AgentName::try_from_str(case.agent_name)
                    .expect("literal validated string is non-empty"),
                slug: Slug::try_from_str(case.slug).expect("literal validated string is non-empty"),
                agent_type: case.runtime,
                birth_branch: BirthBranch::try_from_str(case.birth_branch)
                    .expect("literal validated string is non-empty"),
                parent_branch: BirthBranch::try_from_str(case.parent_branch)
                    .expect("literal validated string is non-empty"),
                working_dir: PathBuf::from(format!(".exo/worktrees/{}", case.agent_name)),
                display_name: case.agent_name.to_string(),
                topology: case.topology,
                model: None,
                effort: None,
            })
            .await
            .expect("identity registration should succeed");

        let mut services = crate::services::Services::test();
        services.agent_resolver = Arc::new(resolver);
        Arc::new(services)
    }

    #[tokio::test]
    async fn non_claude_parent_addressing_matrix_uses_notify_parent_not_literal_parent() {
        let cases = [
            RuntimeParentCase {
                runtime: AgentType::Codex,
                agent_name: "parent-matrix-codex-codex",
                slug: "parent-matrix-codex",
                birth_branch: "main.parent-matrix-codex-codex",
                parent_branch: "main",
                topology: Topology::WorktreePerAgent,
                expected_parent: "root",
            },
            RuntimeParentCase {
                runtime: AgentType::OpenCode,
                agent_name: "parent-matrix-opencode-opencode",
                slug: "parent-matrix-opencode",
                birth_branch: "main.parent-matrix-opencode-opencode",
                parent_branch: "main",
                topology: Topology::WorktreePerAgent,
                expected_parent: "root",
            },
            RuntimeParentCase {
                runtime: AgentType::Codex,
                agent_name: "parent-matrix-codex-codex",
                slug: "parent-matrix-codex",
                birth_branch: "main",
                parent_branch: "main",
                topology: Topology::SharedDir,
                expected_parent: "root",
            },
        ];

        for case in cases {
            let services = services_with_identity(&case).await;
            let handler = EventHandler::new(services.clone(), None);
            let ctx = test_ctx(case.agent_name, case.birth_branch);
            let body = format!("{} notify_parent matrix", case.agent_name);

            let response = crate::effects::EventEffects::notify_parent(
                &handler,
                NotifyParentRequest {
                    agent_id: "".to_string(),
                    status: "success".to_string(),
                    message: body.clone(),
                    override_recipient: None,
                },
                &ctx,
            )
            .await
            .expect("notify_parent should resolve through the event handler");
            assert!(
                response.ack,
                "notify_parent should ack for {:?}",
                case.runtime
            );

            let parent_messages = services
                .inbox_store
                .drain_unread(case.expected_parent)
                .expect("parent inbox drain should succeed");
            assert_eq!(
                parent_messages.len(),
                1,
                "notify_parent should deliver once for {:?}",
                case.runtime
            );
            assert_eq!(parent_messages[0].to_agent, case.expected_parent);
            assert_eq!(
                parent_messages[0].content,
                format!("[from: {}] {}", case.agent_name, body)
            );

            let send_result = crate::effects::EventEffects::send_message(
                &handler,
                SendMessageRequest {
                    recipient: proto_agent_address("parent"),
                    content: "should fail".to_string(),
                    summary: "reserved parent alias".to_string(),
                },
                &ctx,
            )
            .await;
            assert!(
                send_result.is_err(),
                "send_message to literal parent should fail for {:?}",
                case.runtime
            );

            let literal_parent_messages = services
                .inbox_store
                .drain_unread("parent")
                .expect("literal parent inbox drain should succeed");
            assert!(
                literal_parent_messages.is_empty(),
                "literal parent inbox should stay empty for {:?}",
                case.runtime
            );
        }
    }

    #[tokio::test]
    async fn notify_parent_override_parent_alias_resolves_to_real_parent() {
        use exomonad_proto::effects::events::{address::Kind, Address as ProtoAddress};

        let services = Arc::new(crate::services::Services::test());
        let handler = EventHandler::new(services.clone(), None);
        let ctx = test_ctx(
            "m1-breakpoints-step-opencode",
            "main.m1-breakpoints-step-opencode",
        );

        crate::effects::EventEffects::notify_parent(
            &handler,
            NotifyParentRequest {
                agent_id: "".to_string(),
                status: "success".to_string(),
                message: "done".to_string(),
                override_recipient: Some(ProtoAddress {
                    kind: Some(Kind::Agent("parent".to_string())),
                }),
            },
            &ctx,
        )
        .await
        .expect("notify_parent should resolve parent alias");

        let root_messages = services
            .inbox_store
            .drain_unread("root")
            .expect("root inbox drain should succeed");
        assert_eq!(root_messages.len(), 1);
        assert_eq!(root_messages[0].to_agent, "root");
        assert_eq!(
            root_messages[0].content,
            "[from: m1-breakpoints-step-opencode] done"
        );

        let parent_messages = services
            .inbox_store
            .drain_unread("parent")
            .expect("parent inbox drain should succeed");
        assert!(parent_messages.is_empty());
    }

    #[test]
    fn silent_noop_handoff_requires_guidance() {
        assert!(silent_noop_handoff("No commit or PR was created"));
        assert!(silent_noop_handoff("worker exited: no-op"));
        assert!(!silent_noop_handoff("Committed and opened PR #12"));
        assert_eq!(
            bounded_handoff_reason("  no commit was created\nfull task details"),
            "no commit was created"
        );
    }

    #[tokio::test]
    async fn notify_parent_captures_successful_child_handoff() {
        let services = Arc::new(crate::services::Services::test());
        let handler = EventHandler::new(services.clone(), None);
        let ctx = test_ctx("worker-codex", "main.worker-codex");

        let response = crate::effects::EventEffects::notify_parent(
            &handler,
            NotifyParentRequest {
                agent_id: "".to_string(),
                status: "success".to_string(),
                message: "finished the assigned work".to_string(),
                override_recipient: proto_agent_address("root"),
            },
            &ctx,
        )
        .await
        .expect("notify_parent should succeed");

        assert!(response.ack);
        let records = services
            .session_memory
            .list(MemoryFilter::default())
            .expect("memory records should be readable");
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].kind, MemoryKind::ChildHandoff);
        assert_eq!(
            records[0].detail.as_deref(),
            Some("finished the assigned work")
        );
        assert!(records[0]
            .metadata_json
            .as_deref()
            .is_some_and(|metadata| metadata.contains("\"parent\":\"root\"")));
    }

    #[tokio::test]
    async fn notify_parent_captures_explicit_failure_as_blocker() {
        let services = Arc::new(crate::services::Services::test());
        let handler = EventHandler::new(services.clone(), None);
        let ctx = test_ctx("worker-codex", "main.worker-codex");

        let response = crate::effects::EventEffects::notify_parent(
            &handler,
            NotifyParentRequest {
                agent_id: "".to_string(),
                status: "failure".to_string(),
                message: "the implementation is blocked".to_string(),
                override_recipient: proto_agent_address("root"),
            },
            &ctx,
        )
        .await
        .expect("notify_parent should preserve its response on failure status");

        assert!(response.ack);
        let records = services
            .session_memory
            .list(MemoryFilter::default())
            .expect("memory records should be readable");
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].kind, MemoryKind::Blocker);
        assert_eq!(
            records[0].detail.as_deref(),
            Some("the implementation is blocked")
        );
        assert!(records[0]
            .metadata_json
            .as_deref()
            .is_some_and(|metadata| metadata.contains("\"status\":\"failure\"")));
    }

    #[tokio::test]
    async fn notify_parent_ignores_memory_append_failure() {
        use rusqlite::Connection;

        let temp_dir = tempfile::tempdir().expect("tempdir should be created");
        let memory = Arc::new(
            SessionMemoryService::open(temp_dir.path()).expect("memory service should open"),
        );
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();
        services.session_memory = Arc::clone(&memory);
        let services = Arc::new(services);
        let handler = EventHandler::new(services, None);
        let ctx = test_ctx("worker-codex", "main.worker-codex");
        let lock = Connection::open(memory.db_path()).expect("second database connection opens");
        lock.execute_batch("BEGIN EXCLUSIVE")
            .expect("exclusive lock should be acquired");

        let response = crate::effects::EventEffects::notify_parent(
            &handler,
            NotifyParentRequest {
                agent_id: "".to_string(),
                status: "success".to_string(),
                message: "delivery remains authoritative".to_string(),
                override_recipient: proto_agent_address("root"),
            },
            &ctx,
        )
        .await
        .expect("capture failure must not fail notify_parent");

        assert!(response.ack);
        lock.execute_batch("ROLLBACK")
            .expect("exclusive lock should be released");
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
            model: None,
            effort: None,
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

    #[test]
    fn leaf_under_root_notifies_root_not_top_level_branch() {
        // A leaf spawned directly under root lives on `main.<leaf>`; its parent
        // branch is the top-level `main`, which hosts the root TL. The recipient
        // must resolve to the AgentName `root` (root's inbox drain key), not the
        // raw branch name `main` — otherwise the notification is undeliverable.
        let agent_name = AgentName::try_from_str("fix-ci-fmt-opencode")
            .expect("literal validated string is non-empty");
        let birth_branch = BirthBranch::try_from_str("main.fix-ci-fmt-opencode")
            .expect("literal validated string is non-empty");

        assert_eq!(
            structural_parent_session_id(&agent_name, &birth_branch, None),
            "root"
        );
    }
}
