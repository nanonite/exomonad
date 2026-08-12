//! OpenCode adapter for durable guidance and plugin/session acceptance.
//!
//! OpenCode hook completion is transport evidence only. The adapter accepts a
//! batch only when a session event carries the exact batch and invocation
//! correlation required by the queue.

use super::guidance_adapters::{
    AcceptanceConfidence, AcceptanceKind, BoundaryEvidence, GuidanceRuntimeAdapter,
    RuntimeAcceptanceEvidence, RuntimeKind, TransportAttempt, TransportOutcome,
};
use super::guidance_queue::GuidanceBatch;
use super::tmux_events::{self, InjectionOptions};
use anyhow::{bail, Result};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// Session event kinds that are strong enough to acknowledge a batch.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OpenCodeSessionEventKind {
    MessageAccepted,
}

/// Batch-correlated event emitted by an OpenCode plugin/session bridge.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenCodeSessionEvent {
    pub batch_id: String,
    pub agent_id: String,
    pub session_id: String,
    pub invocation_id: String,
    pub generation: u64,
    pub event_kind: OpenCodeSessionEventKind,
    pub correlation_id: String,
}

/// Source of positive OpenCode session evidence.
///
/// Implementations must not report a hook exit or tool completion as an
/// acceptance event. The returned event must be tied to the queried batch.
#[async_trait]
pub trait OpenCodeSessionEventSource: Send + Sync {
    async fn acceptance_event(&self, batch_id: &str) -> Result<Option<OpenCodeSessionEvent>>;
}

/// Whole-batch OpenCode transport, separated from session acceptance.
#[async_trait]
pub trait OpenCodeTransport: Send + Sync {
    async fn submit(
        &self,
        target: &str,
        project_dir: &Path,
        payload: &str,
    ) -> Result<TransportAttempt>;
}

struct NativeOpenCodeTransport;

#[async_trait]
impl OpenCodeTransport for NativeOpenCodeTransport {
    async fn submit(
        &self,
        target: &str,
        project_dir: &Path,
        payload: &str,
    ) -> Result<TransportAttempt> {
        match tmux_events::inject_input_with_options(
            target,
            payload,
            project_dir,
            InjectionOptions::inline_submit(),
        )
        .await
        {
            Ok(()) => Ok(TransportAttempt::success("opencode_tmux")),
            Err(error) => Ok(TransportAttempt::failure(
                "opencode_tmux",
                error.to_string(),
            )),
        }
    }
}

struct NoSessionEvents;

#[async_trait]
impl OpenCodeSessionEventSource for NoSessionEvents {
    async fn acceptance_event(&self, _batch_id: &str) -> Result<Option<OpenCodeSessionEvent>> {
        Ok(None)
    }
}

/// Runtime adapter for OpenCode's FIFO and plugin/session acceptance bridge.
pub struct OpenCodeAdapter {
    target_agent: String,
    tmux_target: String,
    project_dir: PathBuf,
    consumer_id: String,
    boundary: BoundaryEvidence,
    transport: Arc<dyn OpenCodeTransport>,
    session_events: Arc<dyn OpenCodeSessionEventSource>,
}

impl OpenCodeAdapter {
    /// Create an adapter with tmux transport and fail-closed session evidence.
    /// A plugin bridge should be supplied with [`Self::with_components`] before
    /// exact runtime acceptance can be observed.
    pub fn new(
        target_agent: impl Into<String>,
        tmux_target: impl Into<String>,
        project_dir: impl Into<PathBuf>,
        consumer_id: impl Into<String>,
        boundary: BoundaryEvidence,
    ) -> Self {
        Self::with_components(
            target_agent,
            tmux_target,
            project_dir,
            consumer_id,
            boundary,
            Arc::new(NativeOpenCodeTransport),
            Arc::new(NoSessionEvents),
        )
    }

    /// Build an adapter with runtime-provided transport and session evidence.
    pub fn with_components(
        target_agent: impl Into<String>,
        tmux_target: impl Into<String>,
        project_dir: impl Into<PathBuf>,
        consumer_id: impl Into<String>,
        boundary: BoundaryEvidence,
        transport: Arc<dyn OpenCodeTransport>,
        session_events: Arc<dyn OpenCodeSessionEventSource>,
    ) -> Self {
        Self {
            target_agent: target_agent.into(),
            tmux_target: tmux_target.into(),
            project_dir: project_dir.into(),
            consumer_id: consumer_id.into(),
            boundary,
            transport,
            session_events,
        }
    }

    fn batch_payload(batch: &GuidanceBatch) -> Result<String> {
        if batch.items.is_empty() {
            bail!("OpenCode adapter cannot submit an empty guidance batch");
        }
        Ok(batch
            .items
            .iter()
            .map(|item| item.content.as_str())
            .collect::<Vec<_>>()
            .join("\n\n"))
    }

    fn matches_batch_identity(batch: &GuidanceBatch, event: &OpenCodeSessionEvent) -> Option<()> {
        let invocation_id = batch.identity.invocation_id.as_deref()?;
        let generation = batch.identity.generation?;
        (event.batch_id == batch.batch_id
            && event.agent_id == batch.agent_id
            && event.invocation_id == invocation_id
            && event.generation == generation
            && !event.session_id.trim().is_empty()
            && !event.correlation_id.trim().is_empty()
            && event.event_kind == OpenCodeSessionEventKind::MessageAccepted)
            .then_some(())
    }
}

#[async_trait]
impl GuidanceRuntimeAdapter for OpenCodeAdapter {
    fn runtime(&self) -> RuntimeKind {
        RuntimeKind::OpenCode
    }

    fn target_agent(&self) -> &str {
        &self.target_agent
    }

    async fn report_boundary(&self) -> Result<BoundaryEvidence> {
        Ok(self.boundary.clone())
    }

    async fn submit_batch(&self, batch: &GuidanceBatch) -> Result<TransportAttempt> {
        if batch.agent_id != self.target_agent {
            bail!(
                "guidance batch targets `{}`, adapter targets `{}`",
                batch.agent_id,
                self.target_agent
            );
        }
        let payload = Self::batch_payload(batch)?;
        self.transport
            .submit(&self.tmux_target, &self.project_dir, &payload)
            .await
    }

    async fn acceptance_evidence(
        &self,
        batch: &GuidanceBatch,
        transport: &TransportAttempt,
    ) -> Result<Option<RuntimeAcceptanceEvidence>> {
        if transport.outcome != TransportOutcome::Success {
            return Ok(None);
        }
        let Some(event) = self
            .session_events
            .acceptance_event(&batch.batch_id)
            .await?
        else {
            return Ok(None);
        };
        if Self::matches_batch_identity(batch, &event).is_none()
            || self.boundary.invocation_id.as_deref() != Some(event.invocation_id.as_str())
            || self.boundary.generation != Some(event.generation)
        {
            return Ok(None);
        }
        Ok(Some(RuntimeAcceptanceEvidence {
            batch_id: batch.batch_id.clone(),
            agent_id: batch.agent_id.clone(),
            queue_class: batch.queue_class.as_str().to_string(),
            item_ids: batch
                .items
                .iter()
                .map(|item| item.item_id.clone())
                .collect(),
            consumer_id: self.consumer_id.clone(),
            invocation_id: Some(event.invocation_id),
            generation: Some(event.generation),
            evidence_kind: AcceptanceKind::PluginSession,
            confidence: AcceptanceConfidence::Exact,
            correlation_id: Some(event.correlation_id),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::{GuidanceIdentity, GuidanceItem, GuidanceState, QueueClass};
    use serde_json::Value;

    struct FakeTransport;

    #[async_trait]
    impl OpenCodeTransport for FakeTransport {
        async fn submit(
            &self,
            _target: &str,
            _project_dir: &Path,
            payload: &str,
        ) -> Result<TransportAttempt> {
            assert_eq!(payload, "first\n\nsecond");
            Ok(TransportAttempt::success("opencode_plugin"))
        }
    }

    struct FakeSessionEvents {
        event: Option<OpenCodeSessionEvent>,
    }

    #[async_trait]
    impl OpenCodeSessionEventSource for FakeSessionEvents {
        async fn acceptance_event(&self, _batch_id: &str) -> Result<Option<OpenCodeSessionEvent>> {
            Ok(self.event.clone())
        }
    }

    fn batch() -> GuidanceBatch {
        GuidanceBatch {
            batch_id: "batch-1".to_string(),
            agent_id: "agent-opencode".to_string(),
            queue_class: QueueClass::Steering,
            queue_seq: 1,
            state: GuidanceState::Leased,
            acceptance_confidence: AcceptanceConfidence::Unknown,
            created_at: 1,
            available_at: 1,
            lease_owner: Some("consumer-1".to_string()),
            lease_expires_at: Some(60),
            attempt_count: 0,
            accepted_at: None,
            terminal_at: None,
            terminal_reason: None,
            identity: GuidanceIdentity {
                invocation_id: Some("invocation-1".to_string()),
                generation: Some(2),
                ..GuidanceIdentity::default()
            },
            source_message_id: None,
            idempotency_key: None,
            items: vec![
                GuidanceItem {
                    item_id: "item-1".to_string(),
                    position: 0,
                    from_agent: "root".to_string(),
                    content: "first".to_string(),
                    summary: None,
                    injection_options: Value::Null,
                },
                GuidanceItem {
                    item_id: "item-2".to_string(),
                    position: 1,
                    from_agent: "root".to_string(),
                    content: "second".to_string(),
                    summary: None,
                    injection_options: Value::Null,
                },
            ],
        }
    }

    fn event() -> OpenCodeSessionEvent {
        OpenCodeSessionEvent {
            batch_id: "batch-1".to_string(),
            agent_id: "agent-opencode".to_string(),
            session_id: "session-1".to_string(),
            invocation_id: "invocation-1".to_string(),
            generation: 2,
            event_kind: OpenCodeSessionEventKind::MessageAccepted,
            correlation_id: "plugin-event-1".to_string(),
        }
    }

    fn adapter(event: Option<OpenCodeSessionEvent>) -> OpenCodeAdapter {
        OpenCodeAdapter::with_components(
            "agent-opencode",
            "%43",
            ".",
            "consumer-1",
            BoundaryEvidence {
                agent_id: "agent-opencode".to_string(),
                phase: super::super::guidance_adapters::BoundaryPhase::TurnFinished,
                invocation_id: Some("invocation-1".to_string()),
                generation: Some(2),
                correlation_id: Some("turn-1".to_string()),
            },
            Arc::new(FakeTransport),
            Arc::new(FakeSessionEvents { event }),
        )
    }

    #[tokio::test]
    async fn plugin_session_event_accepts_exact_batch() {
        let adapter = adapter(Some(event()));
        let batch = batch();
        let transport = adapter.submit_batch(&batch).await.expect("submit batch");
        let evidence = adapter
            .acceptance_evidence(&batch, &transport)
            .await
            .expect("inspect plugin event")
            .expect("exact OpenCode session acceptance");
        assert_eq!(evidence.evidence_kind, AcceptanceKind::PluginSession);
        assert_eq!(evidence.confidence, AcceptanceConfidence::Exact);
        assert_eq!(evidence.item_ids, vec!["item-1", "item-2"]);
        assert_eq!(evidence.correlation_id.as_deref(), Some("plugin-event-1"));
    }

    #[tokio::test]
    async fn hook_success_without_correlated_session_event_remains_unproven() {
        let adapter_without_event = adapter(None);
        let batch = batch();
        let transport = adapter_without_event
            .submit_batch(&batch)
            .await
            .expect("submit batch");
        assert!(adapter_without_event
            .acceptance_evidence(&batch, &transport)
            .await
            .expect("inspect plugin event")
            .is_none());

        let mut wrong = event();
        wrong.batch_id = "other-batch".to_string();
        let adapter = adapter(Some(wrong));
        let transport = adapter.submit_batch(&batch).await.expect("submit batch");
        assert!(adapter
            .acceptance_evidence(&batch, &transport)
            .await
            .expect("inspect wrong plugin event")
            .is_none());
    }
}
