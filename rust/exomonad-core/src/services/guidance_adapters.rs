//! Runtime-owned boundary and acceptance evidence for durable guidance.
//!
//! An adapter reports facts about a harness. It does not own the model loop and
//! transport success is deliberately represented separately from acceptance.

use super::guidance_queue::GuidanceBatch;
use anyhow::Result;
use serde::{Deserialize, Serialize};

/// Runtime family receiving a guidance batch.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeKind {
    Claude,
    Codex,
    OpenCode,
    Custom,
}

/// Safe boundary reported by the runtime adapter.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BoundaryPhase {
    TurnFinished,
    WouldStop,
    ToolExecuting,
    HardStopped,
}

impl BoundaryPhase {
    pub fn can_offer_steering(self) -> bool {
        matches!(self, Self::TurnFinished | Self::WouldStop)
    }

    pub fn can_offer_follow_up(self) -> bool {
        matches!(self, Self::WouldStop)
    }
}

/// Evidence that a runtime reached a claim-safe turn boundary.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BoundaryEvidence {
    pub agent_id: String,
    pub phase: BoundaryPhase,
    pub invocation_id: Option<String>,
    pub generation: Option<u64>,
    pub correlation_id: Option<String>,
}

impl BoundaryEvidence {
    pub fn turn_finished(agent_id: impl Into<String>) -> Self {
        Self {
            agent_id: agent_id.into(),
            phase: BoundaryPhase::TurnFinished,
            invocation_id: None,
            generation: None,
            correlation_id: None,
        }
    }

    pub fn would_stop(agent_id: impl Into<String>) -> Self {
        Self {
            agent_id: agent_id.into(),
            phase: BoundaryPhase::WouldStop,
            invocation_id: None,
            generation: None,
            correlation_id: None,
        }
    }
}

/// Transport result for one whole-batch submission.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TransportAttempt {
    pub method: String,
    pub outcome: TransportOutcome,
    pub detail: Option<String>,
}

impl TransportAttempt {
    pub fn success(method: impl Into<String>) -> Self {
        Self {
            method: method.into(),
            outcome: TransportOutcome::Success,
            detail: None,
        }
    }

    pub fn failure(method: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            method: method.into(),
            outcome: TransportOutcome::Failed,
            detail: Some(detail.into()),
        }
    }
}

/// Result of asking a transport to submit a batch. This never proves runtime
/// acceptance.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TransportOutcome {
    Success,
    Failed,
}

/// Adapter evidence that the target accepted the exact batch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeAcceptanceEvidence {
    pub batch_id: String,
    pub agent_id: String,
    pub queue_class: String,
    pub item_ids: Vec<String>,
    pub consumer_id: String,
    pub invocation_id: Option<String>,
    pub generation: Option<u64>,
    pub evidence_kind: AcceptanceKind,
    pub confidence: AcceptanceConfidence,
    pub correlation_id: Option<String>,
}

/// Bounded evidence kinds recognized by the queue.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AcceptanceKind {
    MailboxRead,
    RuntimeHook,
    RuntimeBoundary,
    PluginSession,
}

/// Confidence is explicit so transport success cannot be upgraded silently.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AcceptanceConfidence {
    Exact,
    Inferred,
    Unknown,
}

/// Boundary, transport, and acceptance operations supplied by a runtime
/// integration. The trait intentionally has no method that drives a model
/// response loop.
#[async_trait::async_trait]
pub trait GuidanceRuntimeAdapter: Send + Sync {
    fn runtime(&self) -> RuntimeKind;
    fn target_agent(&self) -> &str;
    async fn report_boundary(&self) -> Result<BoundaryEvidence>;
    async fn submit_batch(&self, batch: &GuidanceBatch) -> Result<TransportAttempt>;
    async fn acceptance_evidence(
        &self,
        batch: &GuidanceBatch,
        transport: &TransportAttempt,
    ) -> Result<Option<RuntimeAcceptanceEvidence>>;
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    struct StubAdapter;

    #[async_trait::async_trait]
    impl GuidanceRuntimeAdapter for StubAdapter {
        fn runtime(&self) -> RuntimeKind {
            RuntimeKind::Custom
        }

        fn target_agent(&self) -> &str {
            "agent"
        }

        async fn report_boundary(&self) -> Result<BoundaryEvidence> {
            Ok(BoundaryEvidence::turn_finished(self.target_agent()))
        }

        async fn submit_batch(&self, _batch: &GuidanceBatch) -> Result<TransportAttempt> {
            Ok(TransportAttempt::success("test"))
        }

        async fn acceptance_evidence(
            &self,
            _batch: &GuidanceBatch,
            _transport: &TransportAttempt,
        ) -> Result<Option<RuntimeAcceptanceEvidence>> {
            Ok(None)
        }
    }

    #[test]
    fn boundary_phases_only_offer_work_at_safe_points() {
        assert!(BoundaryPhase::TurnFinished.can_offer_steering());
        assert!(BoundaryPhase::WouldStop.can_offer_steering());
        assert!(BoundaryPhase::WouldStop.can_offer_follow_up());
        assert!(!BoundaryPhase::ToolExecuting.can_offer_steering());
        assert!(!BoundaryPhase::ToolExecuting.can_offer_follow_up());
        assert!(!BoundaryPhase::HardStopped.can_offer_steering());
        assert!(!BoundaryPhase::HardStopped.can_offer_follow_up());
        assert!(!BoundaryPhase::TurnFinished.can_offer_follow_up());
    }

    #[test]
    fn acceptance_envelope_round_trips_with_stable_wire_names() {
        let evidence = RuntimeAcceptanceEvidence {
            batch_id: "batch-1".to_string(),
            agent_id: "agent".to_string(),
            queue_class: "steering".to_string(),
            item_ids: vec!["item-1".to_string(), "item-2".to_string()],
            consumer_id: "consumer-1".to_string(),
            invocation_id: Some("invocation-1".to_string()),
            generation: Some(3),
            evidence_kind: AcceptanceKind::RuntimeBoundary,
            confidence: AcceptanceConfidence::Exact,
            correlation_id: Some("boundary-1".to_string()),
        };
        let encoded = serde_json::to_value(&evidence).expect("serialize evidence envelope");
        assert_eq!(
            encoded,
            json!({
                "batch_id": "batch-1",
                "agent_id": "agent",
                "queue_class": "steering",
                "item_ids": ["item-1", "item-2"],
                "consumer_id": "consumer-1",
                "invocation_id": "invocation-1",
                "generation": 3,
                "evidence_kind": "runtime_boundary",
                "confidence": "exact",
                "correlation_id": "boundary-1"
            })
        );
        assert_eq!(
            serde_json::from_value::<RuntimeAcceptanceEvidence>(encoded)
                .expect("deserialize evidence envelope"),
            evidence
        );
    }

    #[test]
    fn transport_success_has_no_acceptance_confidence() {
        let transport = TransportAttempt::success("tmux");
        assert_eq!(transport.outcome, TransportOutcome::Success);
        assert_eq!(
            serde_json::to_value(transport).expect("serialize transport attempt"),
            json!({"method": "tmux", "outcome": "success", "detail": null})
        );
    }

    #[tokio::test]
    async fn adapter_trait_is_object_safe_and_does_not_drive_a_model_loop() {
        let adapter: &dyn GuidanceRuntimeAdapter = &StubAdapter;
        assert_eq!(adapter.runtime(), RuntimeKind::Custom);
        assert_eq!(adapter.target_agent(), "agent");
        assert_eq!(
            adapter.report_boundary().await.unwrap().phase,
            BoundaryPhase::TurnFinished
        );
    }
}
