//! Claude Code Teams adapter for the durable guidance queue.
//!
//! Teams mailbox reads prove mailbox acceptance for the exact batch. They do
//! not claim that Claude used the message in a model context; that distinction
//! remains represented by `AcceptanceKind::MailboxRead`.

use super::guidance_adapters::{
    AcceptanceConfidence, AcceptanceKind, BoundaryEvidence, GuidanceRuntimeAdapter,
    RuntimeAcceptanceEvidence, RuntimeKind, TransportAttempt, TransportOutcome,
};
use super::guidance_queue::GuidanceBatch;
use anyhow::{anyhow, bail, Result};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

trait ClaudeMailbox: Send + Sync {
    fn write_message(
        &self,
        team: &str,
        recipient: &str,
        from: &str,
        content: &str,
        summary: &str,
    ) -> Result<String>;

    fn is_message_read(&self, team: &str, recipient: &str, timestamp: &str) -> bool;
}

struct NativeClaudeMailbox;

impl ClaudeMailbox for NativeClaudeMailbox {
    fn write_message(
        &self,
        team: &str,
        recipient: &str,
        from: &str,
        content: &str,
        summary: &str,
    ) -> Result<String> {
        claude_teams_bridge::write_to_inbox(team, recipient, from, content, summary)
            .map_err(anyhow::Error::from)
    }

    fn is_message_read(&self, team: &str, recipient: &str, timestamp: &str) -> bool {
        claude_teams_bridge::is_message_read(team, recipient, timestamp)
    }
}

/// Runtime adapter that submits one durable guidance batch to Claude Code's
/// native Teams inbox and waits for mailbox-read evidence before acknowledging.
pub struct ClaudeCodeAdapter {
    target_agent: String,
    team_name: String,
    inbox_name: String,
    consumer_id: String,
    boundary: BoundaryEvidence,
    mailbox: Arc<dyn ClaudeMailbox>,
    submitted_timestamps: Mutex<HashMap<String, Vec<String>>>,
}

impl ClaudeCodeAdapter {
    /// Create an adapter backed by the native Claude Teams filesystem.
    pub fn new(
        target_agent: impl Into<String>,
        team_name: impl Into<String>,
        inbox_name: impl Into<String>,
        consumer_id: impl Into<String>,
        boundary: BoundaryEvidence,
    ) -> Self {
        Self::with_mailbox(
            target_agent,
            team_name,
            inbox_name,
            consumer_id,
            boundary,
            Arc::new(NativeClaudeMailbox),
        )
    }

    fn with_mailbox(
        target_agent: impl Into<String>,
        team_name: impl Into<String>,
        inbox_name: impl Into<String>,
        consumer_id: impl Into<String>,
        boundary: BoundaryEvidence,
        mailbox: Arc<dyn ClaudeMailbox>,
    ) -> Self {
        Self {
            target_agent: target_agent.into(),
            team_name: team_name.into(),
            inbox_name: inbox_name.into(),
            consumer_id: consumer_id.into(),
            boundary,
            mailbox,
            submitted_timestamps: Mutex::new(HashMap::new()),
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
        self.submitted_timestamps
            .lock()
            .map_err(|_| anyhow!("Claude adapter submission state is poisoned"))?
            .remove(batch_id);
        Ok(())
    }
}

#[async_trait::async_trait]
impl GuidanceRuntimeAdapter for ClaudeCodeAdapter {
    fn runtime(&self) -> RuntimeKind {
        RuntimeKind::Claude
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
        if batch.items.is_empty() {
            bail!("Claude Teams adapter cannot submit an empty guidance batch");
        }
        self.clear_submission(&batch.batch_id)?;
        let mut timestamps = Vec::with_capacity(batch.items.len());
        for (position, item) in batch.items.iter().enumerate() {
            let summary = item.summary.as_deref().unwrap_or("");
            let timestamp = match self.mailbox.write_message(
                &self.team_name,
                &self.inbox_name,
                &item.from_agent,
                &item.content,
                summary,
            ) {
                Ok(timestamp) => timestamp,
                Err(error) => {
                    self.clear_submission(&batch.batch_id)?;
                    return Ok(TransportAttempt::failure(
                        "claude_teams_inbox",
                        format!("item {position}: {error}"),
                    ));
                }
            };
            timestamps.push(timestamp);
        }
        self.submitted_timestamps
            .lock()
            .map_err(|_| anyhow!("Claude adapter submission state is poisoned"))?
            .insert(batch.batch_id.clone(), timestamps);
        Ok(TransportAttempt::success("claude_teams_inbox"))
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
        let timestamps = self
            .submitted_timestamps
            .lock()
            .map_err(|_| anyhow!("Claude adapter submission state is poisoned"))?
            .get(&batch.batch_id)
            .cloned();
        let Some(timestamps) = timestamps else {
            return Ok(None);
        };
        if timestamps.len() != batch.items.len()
            || !timestamps.iter().all(|timestamp| {
                self.mailbox
                    .is_message_read(&self.team_name, &self.inbox_name, timestamp)
            })
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
            invocation_id: Some(invocation_id),
            generation: Some(generation),
            evidence_kind: AcceptanceKind::MailboxRead,
            confidence: AcceptanceConfidence::Exact,
            correlation_id: Some(format!("teams:{}", batch.batch_id)),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::{GuidanceIdentity, GuidanceItem, GuidanceState, QueueClass};
    use serde_json::Value;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[derive(Default)]
    struct FakeMailbox {
        next_timestamp: AtomicUsize,
        read: Mutex<HashMap<String, bool>>,
    }

    impl FakeMailbox {
        fn mark_all_read(&self) {
            for value in self.read.lock().unwrap().values_mut() {
                *value = true;
            }
        }
    }

    impl ClaudeMailbox for FakeMailbox {
        fn write_message(
            &self,
            _team: &str,
            _recipient: &str,
            _from: &str,
            _content: &str,
            _summary: &str,
        ) -> Result<String> {
            let timestamp = format!(
                "fake-{}",
                self.next_timestamp.fetch_add(1, Ordering::Relaxed)
            );
            self.read.lock().unwrap().insert(timestamp.clone(), false);
            Ok(timestamp)
        }

        fn is_message_read(&self, _team: &str, _recipient: &str, timestamp: &str) -> bool {
            self.read
                .lock()
                .unwrap()
                .get(timestamp)
                .copied()
                .unwrap_or(false)
        }
    }

    fn batch() -> GuidanceBatch {
        GuidanceBatch {
            batch_id: "batch-1".to_string(),
            agent_id: "agent".to_string(),
            queue_class: QueueClass::Steering,
            queue_seq: 4,
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
                    summary: Some("one".to_string()),
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

    fn adapter(mailbox: Arc<FakeMailbox>) -> ClaudeCodeAdapter {
        ClaudeCodeAdapter::with_mailbox(
            "agent",
            "team",
            "agent",
            "consumer-1",
            BoundaryEvidence {
                agent_id: "agent".to_string(),
                phase: super::super::guidance_adapters::BoundaryPhase::TurnFinished,
                invocation_id: Some("invocation-1".to_string()),
                generation: Some(2),
                correlation_id: Some("turn-1".to_string()),
            },
            mailbox,
        )
    }

    #[tokio::test]
    async fn mailbox_read_is_required_before_acceptance_evidence() {
        let mailbox = Arc::new(FakeMailbox::default());
        let adapter = adapter(mailbox.clone());
        let batch = batch();
        let transport = adapter.submit_batch(&batch).await.expect("submit batch");
        assert_eq!(transport.outcome, TransportOutcome::Success);
        assert!(adapter
            .acceptance_evidence(&batch, &transport)
            .await
            .expect("poll mailbox")
            .is_none());

        mailbox.mark_all_read();
        let evidence = adapter
            .acceptance_evidence(&batch, &transport)
            .await
            .expect("poll mailbox")
            .expect("mailbox acceptance");
        assert_eq!(evidence.evidence_kind, AcceptanceKind::MailboxRead);
        assert_eq!(evidence.confidence, AcceptanceConfidence::Exact);
        assert_eq!(evidence.item_ids, vec!["item-1", "item-2"]);
        assert_eq!(evidence.invocation_id.as_deref(), Some("invocation-1"));
        assert_eq!(evidence.generation, Some(2));
    }

    #[tokio::test]
    async fn failed_transport_and_missing_session_correlation_never_accept() {
        let mailbox = Arc::new(FakeMailbox::default());
        let adapter = adapter(mailbox.clone());
        let batch = batch();
        let failed = TransportAttempt::failure("claude_teams_inbox", "write failed");
        assert!(adapter
            .acceptance_evidence(&batch, &failed)
            .await
            .expect("inspect failed transport")
            .is_none());

        let mut no_identity = batch;
        no_identity.identity.invocation_id = None;
        let transport = adapter
            .submit_batch(&no_identity)
            .await
            .expect("submit batch");
        mailbox.mark_all_read();
        assert!(adapter
            .acceptance_evidence(&no_identity, &transport)
            .await
            .expect("inspect uncorrelated mailbox")
            .is_none());
    }
}
