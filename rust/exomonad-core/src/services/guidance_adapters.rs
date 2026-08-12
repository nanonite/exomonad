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
pub trait GuidanceRuntimeAdapter: Send + Sync {
    fn runtime(&self) -> RuntimeKind;
    fn target_agent(&self) -> &str;
    fn report_boundary(&self) -> Result<BoundaryEvidence>;
    fn submit_batch(&self, batch: &GuidanceBatch) -> Result<TransportAttempt>;
    fn acceptance_evidence(
        &self,
        batch: &GuidanceBatch,
        transport: &TransportAttempt,
    ) -> Result<Option<RuntimeAcceptanceEvidence>>;
}
