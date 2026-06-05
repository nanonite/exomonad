//! Inbox effect handler for the `inbox.*` namespace.

use std::sync::Arc;

use async_trait::async_trait;
use exomonad_proto::effects::inbox::*;

use crate::effects::{dispatch_inbox_effect, EffectHandler, EffectResult, InboxEffects, ResultExt};
use crate::services::HasInboxStore;

/// Handles durable inbox effects for the current agent.
pub struct InboxHandler<C> {
    ctx: Arc<C>,
}

impl<C: HasInboxStore + 'static> InboxHandler<C> {
    pub fn new(ctx: Arc<C>) -> Self {
        Self { ctx }
    }
}

#[async_trait]
impl<C: HasInboxStore + 'static> EffectHandler for InboxHandler<C> {
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
impl<C: HasInboxStore + 'static> InboxEffects for InboxHandler<C> {
    async fn check(
        &self,
        _req: InboxCheckEffect,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<InboxCheckResult> {
        let messages = self
            .ctx
            .inbox_store()
            .drain_unread(ctx.agent_name.as_str())
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::{AgentName, BirthBranch};
    use crate::effects::EffectContext;
    use crate::services::Services;

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
}
