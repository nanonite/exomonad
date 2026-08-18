//! Inbox effect handler for the `inbox.*` namespace.

use std::sync::Arc;

use async_trait::async_trait;
use exomonad_proto::effects::inbox::*;

use crate::domain::{AgentName, Slug};
use crate::effects::{dispatch_inbox_effect, EffectHandler, EffectResult, InboxEffects, ResultExt};
use crate::services::{AgentResolver, HasAgentResolver, HasInboxStore};

/// Handles durable inbox effects for the current agent.
pub struct InboxHandler<C> {
    ctx: Arc<C>,
}

impl<C: HasAgentResolver + HasInboxStore + 'static> InboxHandler<C> {
    pub fn new(ctx: Arc<C>) -> Self {
        Self { ctx }
    }
}

#[async_trait]
impl<C: HasAgentResolver + HasInboxStore + 'static> EffectHandler for InboxHandler<C> {
    fn namespace(&self) -> &str {
        "inbox"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        dispatch_inbox_effect(self, effect_type, payload, ctx).await
    }
}

#[async_trait]
impl<C: HasAgentResolver + HasInboxStore + 'static> InboxEffects for InboxHandler<C> {
    async fn check(
        &self,
        _req: InboxCheckEffect,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<InboxCheckResult> {
        let agent_key =
            canonical_check_agent_key(self.ctx.agent_resolver(), ctx.agent_name.as_str()).await;
        let messages = self
            .ctx
            .inbox_store()
            .drain_unread(&agent_key)
            .effect_err("inbox")?;

        Ok(InboxCheckResult {
            messages: messages
                .into_iter()
                .map(|message| InboxMessage {
                    from_agent: message.from_agent,
                    content: message.content,
                    summary: message.summary.unwrap_or_default(),
                    created_at: message.created_at,
                })
                .collect(),
        })
    }
}

async fn canonical_check_agent_key(resolver: &AgentResolver, agent_key: &str) -> String {
    if !agent_key.contains('.') {
        if let Ok(slug) = Slug::try_from_str(agent_key) {
            if let Some(record) = resolver.lookup_by_slug(&slug).await {
                return record.agent_name.to_string();
            }
        }
    }

    if let Ok(name) = AgentName::try_from_str(agent_key) {
        if let Some(record) = resolver.get(&name).await {
            return record.agent_name.to_string();
        }
    }

    agent_key.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch, Slug};
    use crate::effects::EffectContext;
    use crate::services::agent_control::{AgentType, Topology};
    use crate::services::{AgentIdentityRecord, Services};

    fn test_ctx(agent_name: &str) -> EffectContext {
        EffectContext {
            agent_name: AgentName::try_from_str(agent_name)
                .expect("literal validated string is non-empty"),
            birth_branch: BirthBranch::try_from_str("main")
                .expect("literal validated string is non-empty"),
            working_dir: std::path::PathBuf::from("."),
        }
    }

    #[tokio::test]
    async fn check_drains_current_agent_unread_messages() {
        let services = Arc::new(Services::test());
        services
            .inbox_store
            .write_message("sender", "agent-a", "hello", Some("summary"))
            .unwrap();
        services
            .inbox_store
            .write_message("sender", "agent-b", "not yours", Some("other"))
            .unwrap();

        let handler = InboxHandler::new(services.clone());
        let result = handler
            .check(InboxCheckEffect {}, &test_ctx("agent-a"))
            .await
            .unwrap();

        assert_eq!(result.messages.len(), 1);
        assert_eq!(result.messages[0].from_agent, "sender");
        assert_eq!(result.messages[0].content, "hello");
        assert_eq!(result.messages[0].summary, "summary");
        assert!(!services.inbox_store.has_unread("agent-a").unwrap());
        assert!(services.inbox_store.has_unread("agent-b").unwrap());
    }

    #[tokio::test]
    async fn check_canonicalizes_slug_to_registered_agent_name() {
        let services = Arc::new(Services::test());
        services
            .agent_resolver
            .register(AgentIdentityRecord {
                agent_name: AgentName::try_from_str("root-claude")
                    .expect("literal validated string is non-empty"),
                slug: Slug::try_from_str("root").expect("literal validated string is non-empty"),
                agent_type: AgentType::Claude,
                birth_branch: BirthBranch::try_from_str("main")
                    .expect("literal validated string is non-empty"),
                parent_branch: BirthBranch::try_from_str("main")
                    .expect("literal validated string is non-empty"),
                working_dir: std::path::PathBuf::from("."),
                display_name: "root".to_string(),
                topology: Topology::SharedDir,
                model: None,
                effort: None,
                ledger_owned: false,
                slice_id: None,
            })
            .await
            .expect("identity registration should succeed");
        services
            .inbox_store
            .write_message("sender", "root-claude", "wake up", Some("wake"))
            .unwrap();

        let handler = InboxHandler::new(services.clone());
        let result = handler
            .check(InboxCheckEffect {}, &test_ctx("root"))
            .await
            .unwrap();

        assert_eq!(result.messages.len(), 1);
        assert_eq!(result.messages[0].content, "wake up");
        assert!(!services.inbox_store.has_unread("root-claude").unwrap());
    }

    #[tokio::test]
    async fn check_prefers_slug_canonical_name_over_bare_registered_name() {
        let services = Arc::new(Services::test());
        services
            .agent_resolver
            .register(AgentIdentityRecord {
                agent_name: AgentName::try_from_str("root")
                    .expect("literal validated string is non-empty"),
                slug: Slug::try_from_str("root-bare")
                    .expect("literal validated string is non-empty"),
                agent_type: AgentType::Claude,
                birth_branch: BirthBranch::try_from_str("main")
                    .expect("literal validated string is non-empty"),
                parent_branch: BirthBranch::try_from_str("main")
                    .expect("literal validated string is non-empty"),
                working_dir: std::path::PathBuf::from("."),
                display_name: "root".to_string(),
                topology: Topology::SharedDir,
                model: None,
                effort: None,
                ledger_owned: false,
                slice_id: None,
            })
            .await
            .expect("bare root registration should succeed");
        services
            .agent_resolver
            .register(AgentIdentityRecord {
                agent_name: AgentName::try_from_str("root-claude")
                    .expect("literal validated string is non-empty"),
                slug: Slug::try_from_str("root").expect("literal validated string is non-empty"),
                agent_type: AgentType::Claude,
                birth_branch: BirthBranch::try_from_str("main")
                    .expect("literal validated string is non-empty"),
                parent_branch: BirthBranch::try_from_str("main")
                    .expect("literal validated string is non-empty"),
                working_dir: std::path::PathBuf::from("."),
                display_name: "root".to_string(),
                topology: Topology::SharedDir,
                model: None,
                effort: None,
                ledger_owned: false,
                slice_id: None,
            })
            .await
            .expect("canonical root registration should succeed");
        services
            .inbox_store
            .write_message("sender", "root-claude", "wake up", Some("wake"))
            .unwrap();

        let handler = InboxHandler::new(services.clone());
        let result = handler
            .check(InboxCheckEffect {}, &test_ctx("root"))
            .await
            .unwrap();

        assert_eq!(result.messages.len(), 1);
        assert_eq!(result.messages[0].content, "wake up");
        assert!(!services.inbox_store.has_unread("root-claude").unwrap());
    }

    #[tokio::test]
    async fn check_drains_branch_qualified_mail_for_bare_agent_name() {
        let services = Arc::new(Services::test());
        services
            .inbox_store
            .write_message("sender", "main.agent-a", "hello", Some("summary"))
            .unwrap();

        let handler = InboxHandler::new(services.clone());
        let result = handler
            .check(InboxCheckEffect {}, &test_ctx("agent-a"))
            .await
            .unwrap();

        assert_eq!(result.messages.len(), 1);
        assert_eq!(result.messages[0].from_agent, "sender");
        assert_eq!(result.messages[0].content, "hello");
        assert!(!services.inbox_store.has_unread("agent-a").unwrap());
    }
}
