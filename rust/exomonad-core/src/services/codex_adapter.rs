//! Codex tmux adapter for the durable guidance queue.
//!
//! A tmux write is recorded as transport only. Acceptance requires a later
//! Codex-specific positive TUI signal correlated with the submitted batch and
//! its invocation generation.

use super::guidance_adapters::{
    AcceptanceConfidence, AcceptanceKind, BoundaryEvidence, GuidanceRuntimeAdapter,
    RuntimeAcceptanceEvidence, RuntimeKind, TransportAttempt, TransportOutcome,
};
use super::guidance_queue::GuidanceBatch;
use super::tmux_events::{self, InjectionOptions};
use super::tmux_ipc::TmuxIpc;
use super::tui_consumption::{positive_consumption_signal, RuntimeKind as TuiRuntimeKind};
use anyhow::{anyhow, bail, Result};
use async_trait::async_trait;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

#[async_trait]
trait CodexTransport: Send + Sync {
    async fn submit(
        &self,
        target: &str,
        project_dir: &Path,
        payload: &str,
    ) -> Result<TransportAttempt>;

    async fn capture_pane(&self, target: &str) -> Result<String>;
}

struct NativeCodexTransport;

#[async_trait]
impl CodexTransport for NativeCodexTransport {
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
            Ok(()) => Ok(TransportAttempt::success("codex_tmux")),
            Err(error) => Ok(TransportAttempt::failure("codex_tmux", error.to_string())),
        }
    }

    async fn capture_pane(&self, target: &str) -> Result<String> {
        let session = std::env::var("EXOMONAD_TMUX_SESSION")
            .map_err(|_| anyhow!("EXOMONAD_TMUX_SESSION not set"))?;
        TmuxIpc::new(&session).capture_pane(target).await
    }
}

/// Runtime adapter for Codex's ExoMonad FIFO and tmux turn-boundary path.
pub struct CodexAdapter {
    target_agent: String,
    tmux_target: String,
    project_dir: PathBuf,
    consumer_id: String,
    boundary: BoundaryEvidence,
    transport: Arc<dyn CodexTransport>,
    submitted_before: Mutex<HashMap<String, String>>,
}

impl CodexAdapter {
    /// Create an adapter backed by the process's exact tmux session.
    pub fn new(
        target_agent: impl Into<String>,
        tmux_target: impl Into<String>,
        project_dir: impl Into<PathBuf>,
        consumer_id: impl Into<String>,
        boundary: BoundaryEvidence,
    ) -> Self {
        Self::with_transport(
            target_agent,
            tmux_target,
            project_dir,
            consumer_id,
            boundary,
            Arc::new(NativeCodexTransport),
        )
    }

    fn with_transport(
        target_agent: impl Into<String>,
        tmux_target: impl Into<String>,
        project_dir: impl Into<PathBuf>,
        consumer_id: impl Into<String>,
        boundary: BoundaryEvidence,
        transport: Arc<dyn CodexTransport>,
    ) -> Self {
        Self {
            target_agent: target_agent.into(),
            tmux_target: tmux_target.into(),
            project_dir: project_dir.into(),
            consumer_id: consumer_id.into(),
            boundary,
            transport,
            submitted_before: Mutex::new(HashMap::new()),
        }
    }

    fn correlated_identity(&self, batch: &GuidanceBatch) -> Option<(String, u64)> {
        let invocation_id = batch.identity.invocation_id.clone()?;
        let generation = batch.identity.generation?;
        if self.boundary.invocation_id.as_deref() != Some(invocation_id.as_str())
            || self.boundary.generation != Some(generation)
        {
            return None;
        }
        Some((invocation_id, generation))
    }

    fn clear_submission(&self, batch_id: &str) -> Result<()> {
        self.submitted_before
            .lock()
            .map_err(|_| anyhow!("Codex adapter submission state is poisoned"))?
            .remove(batch_id);
        Ok(())
    }

    fn batch_payload(batch: &GuidanceBatch) -> Result<String> {
        if batch.items.is_empty() {
            bail!("Codex adapter cannot submit an empty guidance batch");
        }
        Ok(batch
            .items
            .iter()
            .map(|item| item.content.as_str())
            .collect::<Vec<_>>()
            .join("\n\n"))
    }
}

#[async_trait]
impl GuidanceRuntimeAdapter for CodexAdapter {
    fn runtime(&self) -> RuntimeKind {
        RuntimeKind::Codex
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
        let before = self.transport.capture_pane(&self.tmux_target).await?;
        let attempt = self
            .transport
            .submit(&self.tmux_target, &self.project_dir, &payload)
            .await?;
        self.clear_submission(&batch.batch_id)?;
        if attempt.outcome == TransportOutcome::Success {
            self.submitted_before
                .lock()
                .map_err(|_| anyhow!("Codex adapter submission state is poisoned"))?
                .insert(batch.batch_id.clone(), before);
        }
        Ok(attempt)
    }

    async fn acceptance_evidence(
        &self,
        batch: &GuidanceBatch,
        transport: &TransportAttempt,
    ) -> Result<Option<RuntimeAcceptanceEvidence>> {
        if transport.outcome != TransportOutcome::Success {
            return Ok(None);
        }
        let Some((invocation_id, generation)) = self.correlated_identity(batch) else {
            return Ok(None);
        };
        let before = self
            .submitted_before
            .lock()
            .map_err(|_| anyhow!("Codex adapter submission state is poisoned"))?
            .get(&batch.batch_id)
            .cloned();
        let Some(before) = before else {
            return Ok(None);
        };
        let after = self.transport.capture_pane(&self.tmux_target).await?;
        if !positive_consumption_signal(TuiRuntimeKind::Codex, &before, &after) {
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
            invocation_id: Some(invocation_id),
            generation: Some(generation),
            evidence_kind: AcceptanceKind::RuntimeBoundary,
            confidence: AcceptanceConfidence::Exact,
            correlation_id: Some(format!("codex:{}", batch.batch_id)),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::{GuidanceIdentity, GuidanceItem, GuidanceState, QueueClass};
    use serde_json::Value;

    struct FakeCodexTransport {
        pane: Mutex<String>,
        produce_signal: bool,
        payloads: Mutex<Vec<String>>,
    }

    #[async_trait]
    impl CodexTransport for FakeCodexTransport {
        async fn submit(
            &self,
            _target: &str,
            _project_dir: &Path,
            payload: &str,
        ) -> Result<TransportAttempt> {
            self.payloads.lock().unwrap().push(payload.to_string());
            if self.produce_signal {
                *self.pane.lock().unwrap() = "assistant tokens codex thinking".to_string();
            }
            Ok(TransportAttempt::success("codex_tmux"))
        }

        async fn capture_pane(&self, _target: &str) -> Result<String> {
            Ok(self.pane.lock().unwrap().clone())
        }
    }

    fn batch() -> GuidanceBatch {
        GuidanceBatch {
            batch_id: "batch-1".to_string(),
            agent_id: "agent-codex".to_string(),
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

    fn adapter(transport: Arc<FakeCodexTransport>) -> CodexAdapter {
        CodexAdapter::with_transport(
            "agent-codex",
            "%42",
            ".",
            "consumer-1",
            BoundaryEvidence {
                agent_id: "agent-codex".to_string(),
                phase: super::super::guidance_adapters::BoundaryPhase::TurnFinished,
                invocation_id: Some("invocation-1".to_string()),
                generation: Some(2),
                correlation_id: Some("turn-1".to_string()),
            },
            transport,
        )
    }

    #[tokio::test]
    async fn tmux_write_is_transport_and_positive_tui_signal_accepts_whole_batch() {
        let transport = Arc::new(FakeCodexTransport {
            pane: Mutex::new("waiting".to_string()),
            produce_signal: true,
            payloads: Mutex::new(Vec::new()),
        });
        let adapter = adapter(transport.clone());
        let batch = batch();
        let attempt = adapter.submit_batch(&batch).await.expect("submit batch");
        assert_eq!(attempt.outcome, TransportOutcome::Success);
        assert_eq!(
            transport.payloads.lock().unwrap().as_slice(),
            ["first\n\nsecond"]
        );

        let evidence = adapter
            .acceptance_evidence(&batch, &attempt)
            .await
            .expect("inspect Codex TUI")
            .expect("positive Codex TUI signal");
        assert_eq!(evidence.evidence_kind, AcceptanceKind::RuntimeBoundary);
        assert_eq!(evidence.confidence, AcceptanceConfidence::Exact);
        assert_eq!(evidence.item_ids, vec!["item-1", "item-2"]);
        assert_eq!(evidence.invocation_id.as_deref(), Some("invocation-1"));
        assert_eq!(evidence.generation, Some(2));
    }

    #[tokio::test]
    async fn tmux_success_without_positive_signal_remains_unproven() {
        let transport = Arc::new(FakeCodexTransport {
            pane: Mutex::new("waiting".to_string()),
            produce_signal: false,
            payloads: Mutex::new(Vec::new()),
        });
        let adapter = adapter(transport);
        let batch = batch();
        let attempt = adapter.submit_batch(&batch).await.expect("submit batch");
        assert!(adapter
            .acceptance_evidence(&batch, &attempt)
            .await
            .expect("inspect Codex TUI")
            .is_none());
    }
}
