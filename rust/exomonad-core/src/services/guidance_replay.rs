//! Deterministic, body-free replay of durable guidance queue state.

use super::guidance_queue::{GuidanceIdentity, GuidanceState, QueueClass};
use super::immutable_ledger::LedgerRecord;
use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

const STATE_EVENT: &str = "inbox.state_changed";

/// The queue fields that can be reconstructed from the immutable ledger.
///
/// Item bodies are intentionally absent. They remain in SQLite/L1 local
/// storage and are not part of this aggregate replay projection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GuidanceReplayBatch {
    pub batch_id: String,
    pub agent_id: String,
    pub queue_class: QueueClass,
    pub queue_seq: i64,
    pub state: GuidanceState,
    pub item_ids: Vec<String>,
    pub identity: GuidanceIdentity,
}

impl GuidanceReplayBatch {
    pub fn is_terminal(&self) -> bool {
        self.state.is_terminal()
    }
}

/// Stable replay result keyed by durable batch identity.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct GuidanceReplayProjection {
    pub batches: BTreeMap<String, GuidanceReplayBatch>,
}

/// Reconstruct durable guidance state from the canonical ledger order.
pub fn replay_guidance_batches(records: &[LedgerRecord]) -> Result<GuidanceReplayProjection> {
    let mut projection = GuidanceReplayProjection::default();
    for record in records {
        if record.event.event_type != STATE_EVENT {
            continue;
        }
        let Some(data) = record.event.data.as_object() else {
            continue;
        };
        let Some(operation) = data.get("operation").and_then(Value::as_str) else {
            continue;
        };
        if !is_queue_state_operation(operation) {
            continue;
        }
        let batch_id = required_string(data, "batch_id")?;
        if operation == "enqueue" {
            if projection.batches.contains_key(&batch_id) {
                continue;
            }
            let batch = new_batch(data, batch_id.clone())?;
            projection.batches.insert(batch_id, batch);
            continue;
        }

        let batch = projection
            .batches
            .get_mut(&batch_id)
            .with_context(|| format!("guidance state event precedes enqueue for {batch_id}"))?;
        update_metadata(batch, data)?;
        let next_state = target_state(operation, data)?;
        if let Some(declared_state) = declared_state(data)? {
            if declared_state != next_state {
                bail!(
                    "guidance operation {operation} declares {} but replays to {}",
                    declared_state.as_str(),
                    next_state.as_str()
                );
            }
        }
        validate_transition(batch.state, next_state, operation)?;
        batch.state = next_state;
    }
    Ok(projection)
}

fn is_queue_state_operation(operation: &str) -> bool {
    matches!(
        operation,
        "enqueue"
            | "claim"
            | "transport_submitted"
            | "acknowledge"
            | "release_for_retry"
            | "abandon"
            | "lease_expired"
            | "cancel"
    )
}

fn new_batch(
    data: &serde_json::Map<String, Value>,
    batch_id: String,
) -> Result<GuidanceReplayBatch> {
    let item_ids = item_ids(data)?;
    Ok(GuidanceReplayBatch {
        batch_id,
        agent_id: required_string(data, "agent_id")?,
        queue_class: QueueClass::parse(&required_string(data, "queue_class")?)?,
        queue_seq: required_i64(data, "queue_seq")?,
        state: GuidanceState::Pending,
        item_ids,
        identity: identity(data),
    })
}

fn update_metadata(
    batch: &mut GuidanceReplayBatch,
    data: &serde_json::Map<String, Value>,
) -> Result<()> {
    if let Some(agent_id) = optional_string(data, "agent_id") {
        if agent_id != batch.agent_id {
            bail!(
                "guidance batch {} changed target agent during replay",
                batch.batch_id
            );
        }
    }
    if let Some(queue_class) = optional_string(data, "queue_class") {
        if QueueClass::parse(&queue_class)? != batch.queue_class {
            bail!(
                "guidance batch {} changed queue class during replay",
                batch.batch_id
            );
        }
    }
    if let Some(queue_seq) = optional_i64(data, "queue_seq") {
        if queue_seq != batch.queue_seq {
            bail!(
                "guidance batch {} changed queue sequence during replay",
                batch.batch_id
            );
        }
    }
    if let Some(next_item_ids) = optional_item_ids(data)? {
        if !batch.item_ids.is_empty() && next_item_ids != batch.item_ids {
            bail!(
                "guidance batch {} changed item identities during replay",
                batch.batch_id
            );
        }
        batch.item_ids = next_item_ids;
    }
    merge_identity(&mut batch.identity, data);
    Ok(())
}

fn target_state(operation: &str, data: &serde_json::Map<String, Value>) -> Result<GuidanceState> {
    match operation {
        "claim" => Ok(GuidanceState::Leased),
        "transport_submitted" => Ok(GuidanceState::Submitted),
        "acknowledge" => Ok(GuidanceState::Accepted),
        "cancel" => Ok(GuidanceState::Cancelled),
        "release_for_retry" | "abandon" | "lease_expired" => declared_state(data)?
            .with_context(|| format!("guidance operation {operation} lacks state")),
        _ => bail!("unsupported guidance state operation {operation}"),
    }
}

fn declared_state(data: &serde_json::Map<String, Value>) -> Result<Option<GuidanceState>> {
    data.get("state")
        .map(|value| {
            value
                .as_str()
                .with_context(|| "guidance state must be a string".to_string())
                .and_then(GuidanceState::parse)
                .map(Some)
        })
        .transpose()
        .map(|value| value.flatten())
}

fn validate_transition(current: GuidanceState, next: GuidanceState, operation: &str) -> Result<()> {
    let valid = matches!(
        (current, next),
        (
            GuidanceState::Pending,
            GuidanceState::Leased | GuidanceState::Cancelled
        ) | (
            GuidanceState::Leased,
            GuidanceState::Submitted | GuidanceState::Accepted
        ) | (
            GuidanceState::Leased,
            GuidanceState::Pending | GuidanceState::Abandoned
        ) | (GuidanceState::Leased, GuidanceState::Cancelled)
            | (GuidanceState::Submitted, GuidanceState::Accepted)
            | (
                GuidanceState::Submitted,
                GuidanceState::Pending | GuidanceState::Abandoned
            )
            | (GuidanceState::Submitted, GuidanceState::Cancelled)
    );
    if valid {
        Ok(())
    } else {
        bail!(
            "invalid guidance replay transition {} -> {} for {operation}",
            current.as_str(),
            next.as_str()
        )
    }
}

fn identity(data: &serde_json::Map<String, Value>) -> GuidanceIdentity {
    GuidanceIdentity {
        run_id: optional_string(data, "run_id"),
        session_id: optional_string(data, "session_id"),
        invocation_id: optional_string(data, "invocation_id"),
        generation: data.get("generation").and_then(Value::as_u64),
        runtime: optional_string(data, "runtime"),
        harness: optional_string(data, "harness"),
        role: optional_string(data, "role"),
    }
}

fn merge_identity(identity: &mut GuidanceIdentity, data: &serde_json::Map<String, Value>) {
    if identity.run_id.is_none() {
        identity.run_id = optional_string(data, "run_id");
    }
    if identity.session_id.is_none() {
        identity.session_id = optional_string(data, "session_id");
    }
    if identity.invocation_id.is_none() {
        identity.invocation_id = optional_string(data, "invocation_id");
    }
    if identity.generation.is_none() {
        identity.generation = data.get("generation").and_then(Value::as_u64);
    }
    if identity.runtime.is_none() {
        identity.runtime = optional_string(data, "runtime");
    }
    if identity.harness.is_none() {
        identity.harness = optional_string(data, "harness");
    }
    if identity.role.is_none() {
        identity.role = optional_string(data, "role");
    }
}

fn item_ids(data: &serde_json::Map<String, Value>) -> Result<Vec<String>> {
    optional_item_ids(data)?.with_context(|| "guidance enqueue lacks item_ids".to_string())
}

fn optional_item_ids(data: &serde_json::Map<String, Value>) -> Result<Option<Vec<String>>> {
    let Some(value) = data.get("item_ids") else {
        return Ok(data
            .get("item_id")
            .and_then(Value::as_str)
            .map(|item_id| vec![item_id.to_string()]));
    };
    let items = value
        .as_array()
        .with_context(|| "guidance item_ids must be an array".to_string())?
        .iter()
        .map(|item| {
            item.as_str()
                .map(str::to_string)
                .with_context(|| "guidance item_ids must contain strings".to_string())
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(Some(items))
}

fn required_string(data: &serde_json::Map<String, Value>, field: &str) -> Result<String> {
    optional_string(data, field).with_context(|| format!("guidance event lacks {field}"))
}

fn optional_string(data: &serde_json::Map<String, Value>, field: &str) -> Option<String> {
    data.get(field).and_then(Value::as_str).map(str::to_string)
}

fn required_i64(data: &serde_json::Map<String, Value>, field: &str) -> Result<i64> {
    optional_i64(data, field).with_context(|| format!("guidance event lacks {field}"))
}

fn optional_i64(data: &serde_json::Map<String, Value>, field: &str) -> Option<i64> {
    data.get(field).and_then(|value| {
        value
            .as_i64()
            .or_else(|| value.as_u64().and_then(|value| i64::try_from(value).ok()))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::guidance_adapters::{
        AcceptanceConfidence, AcceptanceKind, BoundaryEvidence, RuntimeAcceptanceEvidence,
    };
    use crate::services::guidance_queue::{
        GuidanceBatchRequest, GuidanceConsumer, GuidanceItemInput,
    };
    use crate::services::{GuidanceIdentity, InboxStore, LedgerWriter};
    use anyhow::Result;
    use rusqlite::params;
    use serde_json::json;
    use tempfile::TempDir;

    fn request(key: &str) -> GuidanceBatchRequest {
        GuidanceBatchRequest {
            agent_id: "agent".to_string(),
            queue_class: QueueClass::Steering,
            items: vec![GuidanceItemInput {
                from_agent: "root".to_string(),
                content: format!("body-{key}"),
                summary: Some("bounded summary".to_string()),
                injection_options: json!({"submit": true}),
            }],
            identity: GuidanceIdentity {
                session_id: Some("session-replay".to_string()),
                invocation_id: Some("invocation-replay".to_string()),
                generation: Some(1),
                ..GuidanceIdentity::default()
            },
            idempotency_key: Some(key.to_string()),
            source_message_id: None,
        }
    }

    fn consumer() -> GuidanceConsumer {
        GuidanceConsumer {
            consumer_id: "consumer-replay".to_string(),
            invocation_id: Some("invocation-replay".to_string()),
            generation: Some(1),
        }
    }

    fn acceptance(batch: &super::GuidanceReplayBatch) -> RuntimeAcceptanceEvidence {
        RuntimeAcceptanceEvidence {
            batch_id: batch.batch_id.clone(),
            agent_id: batch.agent_id.clone(),
            queue_class: batch.queue_class.as_str().to_string(),
            item_ids: batch.item_ids.clone(),
            consumer_id: "consumer-replay".to_string(),
            invocation_id: batch.identity.invocation_id.clone(),
            generation: batch.identity.generation,
            evidence_kind: AcceptanceKind::RuntimeBoundary,
            confidence: AcceptanceConfidence::Exact,
            correlation_id: Some("correlation-replay".to_string()),
        }
    }

    #[test]
    fn replay_matches_sqlite_pending_accepted_and_terminal_states() -> Result<()> {
        let directory = TempDir::new()?;
        let store = InboxStore::open(directory.path())?;
        let pending = store.enqueue_batch(request("pending"))?.batch;
        let accepted = store.enqueue_batch(request("accepted"))?.batch;
        let cancelled = store.enqueue_batch(request("cancelled"))?.batch;
        let abandoned = store.enqueue_batch(request("abandoned"))?.batch;
        let consumer = consumer();
        store.connection()?.execute(
            "UPDATE guidance_batches SET available_at = ?1 WHERE batch_id = ?2",
            params![
                crate::services::inbox_store::now_epoch_secs() + 600,
                pending.batch_id
            ],
        )?;
        store.cancel_batch(&cancelled.batch_id, "operator_stop")?;

        let accepted_claim = store
            .claim_next(&BoundaryEvidence::turn_finished("agent"), &consumer, 60)?
            .context("accepted claim")?;
        assert_eq!(accepted_claim.batch_id, accepted.batch_id);
        store.acknowledge_runtime(&acceptance(&GuidanceReplayBatch {
            batch_id: accepted_claim.batch_id.clone(),
            agent_id: accepted_claim.agent_id.clone(),
            queue_class: accepted_claim.queue_class,
            queue_seq: accepted_claim.queue_seq,
            state: accepted_claim.state,
            item_ids: accepted_claim
                .items
                .iter()
                .map(|item| item.item_id.clone())
                .collect(),
            identity: accepted_claim.identity.clone(),
        }))?;

        let abandoned_claim = store
            .claim_next(&BoundaryEvidence::turn_finished("agent"), &consumer, 60)?
            .context("abandoned claim")?;
        assert_eq!(abandoned_claim.batch_id, abandoned.batch_id);
        store.connection()?.execute(
            "UPDATE guidance_batches SET attempt_count = 8 WHERE batch_id = ?1",
            params![abandoned.batch_id],
        )?;
        store.release_for_retry(
            &abandoned.batch_id,
            &consumer.consumer_id,
            "retry budget exhausted",
            crate::services::inbox_store::now_epoch_secs(),
        )?;

        let records = LedgerWriter::open_project(directory.path())?.read_events()?;
        let projection = replay_guidance_batches(&records)?;
        let connection = store.connection()?;
        let mut statement = connection.prepare(
            "SELECT batch_id, agent_id, queue_class, queue_seq, state
             FROM guidance_batches ORDER BY batch_id",
        )?;
        let sqlite = statement
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, String>(4)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let replayed = projection
            .batches
            .values()
            .map(|batch| {
                (
                    batch.batch_id.clone(),
                    batch.agent_id.clone(),
                    batch.queue_class.as_str().to_string(),
                    batch.queue_seq,
                    batch.state.as_str().to_string(),
                )
            })
            .collect::<Vec<_>>();
        assert_eq!(replayed, sqlite);
        assert_eq!(
            projection.batches[&pending.batch_id].state,
            GuidanceState::Pending
        );
        assert_eq!(
            projection.batches[&accepted.batch_id].state,
            GuidanceState::Accepted
        );
        assert_eq!(
            projection.batches[&cancelled.batch_id].state,
            GuidanceState::Cancelled
        );
        assert_eq!(
            projection.batches[&abandoned.batch_id].state,
            GuidanceState::Abandoned
        );
        assert!(projection.batches[&cancelled.batch_id].is_terminal());
        assert!(projection.batches[&abandoned.batch_id].is_terminal());
        let projection_json = serde_json::to_string(&projection)?;
        assert!(!projection_json.contains("body-pending"));
        assert!(!projection_json.contains("bounded summary"));
        Ok(())
    }
}
