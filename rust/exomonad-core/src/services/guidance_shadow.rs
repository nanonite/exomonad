//! Shadow comparison for the legacy inbox and durable guidance projections.
//!
//! Compatibility delivery writes both projections in one transaction. This
//! module compares the resulting rows without treating the legacy `read_at`
//! flag as proof of runtime acceptance and without putting message bodies in
//! the comparison result or its ledger event.

use super::guidance_queue::{GuidanceBatch, GuidanceState, QueueClass};
use super::inbox_store::InboxStore;
use anyhow::{Context, Result};
use rusqlite::OptionalExtension;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ShadowLegacyProjection {
    pub message_id: i64,
    pub from_agent: String,
    pub to_agent: String,
    pub content_digest: String,
    pub summary_digest: Option<String>,
    pub read: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ShadowQueueProjection {
    pub batch_id: String,
    pub source_message_id: Option<i64>,
    pub agent_id: String,
    pub queue_class: QueueClass,
    pub queue_seq: i64,
    pub state: GuidanceState,
    pub item_count: usize,
    pub item_from_agent: Option<String>,
    pub item_content_digest: Option<String>,
    pub item_summary_digest: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ShadowDiff {
    QueueBatchMissing,
    LegacySourcePointerMissing,
    LegacyMessageMissing,
    TargetAgentMismatch,
    SenderMismatch,
    ContentMismatch,
    SummaryMismatch,
    BatchShapeMismatch,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ShadowComparison {
    pub queue: Option<ShadowQueueProjection>,
    pub legacy: Option<ShadowLegacyProjection>,
    pub diffs: Vec<ShadowDiff>,
}

impl ShadowComparison {
    pub fn in_sync(&self) -> bool {
        self.diffs.is_empty()
    }

    pub fn diff_count(&self) -> usize {
        self.diffs.len()
    }
}

/// Compare one durable batch with its compatibility inbox projection.
pub fn compare_batch(store: &InboxStore, batch_id: &str) -> Result<ShadowComparison> {
    let Some(batch) = store.inspect_batch(batch_id)? else {
        return Ok(ShadowComparison {
            queue: None,
            legacy: None,
            diffs: vec![ShadowDiff::QueueBatchMissing],
        });
    };

    compare_loaded_batch(store, &batch)
}

pub(crate) fn emit_comparison(store: &InboxStore, comparison: &ShadowComparison) {
    super::state_mirror::append_state_change(
        store.db_path(),
        "inbox.state_changed",
        serde_json::json!({
            "operation": "shadow_compare",
            "batch_id": comparison.queue.as_ref().map(|queue| &queue.batch_id),
            "source_message_id": comparison
                .queue
                .as_ref()
                .and_then(|queue| queue.source_message_id),
            "in_sync": comparison.in_sync(),
            "diff_count": comparison.diff_count(),
            "diffs": &comparison.diffs,
        }),
    );
}

fn compare_loaded_batch(store: &InboxStore, batch: &GuidanceBatch) -> Result<ShadowComparison> {
    let queue = ShadowQueueProjection {
        batch_id: batch.batch_id.clone(),
        source_message_id: batch.source_message_id,
        agent_id: batch.agent_id.clone(),
        queue_class: batch.queue_class,
        queue_seq: batch.queue_seq,
        state: batch.state,
        item_count: batch.items.len(),
        item_from_agent: batch.items.first().map(|item| item.from_agent.clone()),
        item_content_digest: batch.items.first().map(|item| digest(&item.content)),
        item_summary_digest: batch
            .items
            .first()
            .and_then(|item| item.summary.as_deref().map(digest)),
    };
    let mut comparison = ShadowComparison {
        queue: Some(queue),
        legacy: None,
        diffs: Vec::new(),
    };

    let Some(source_message_id) = batch.source_message_id else {
        comparison
            .diffs
            .push(ShadowDiff::LegacySourcePointerMissing);
        return Ok(comparison);
    };

    let legacy = {
        let conn = store.connection()?;
        conn.query_row(
            "SELECT id, from_agent, to_agent, content, summary, read_at
             FROM messages WHERE id = ?1",
            [source_message_id],
            |row| {
                Ok(ShadowLegacyProjection {
                    message_id: row.get(0)?,
                    from_agent: row.get(1)?,
                    to_agent: row.get(2)?,
                    content_digest: digest(&row.get::<_, String>(3)?),
                    summary_digest: row.get::<_, Option<String>>(4)?.as_deref().map(digest),
                    read: row.get::<_, Option<i64>>(5)?.is_some(),
                })
            },
        )
        .optional()
        .context("failed to load legacy shadow projection")?
    };

    let Some(legacy) = legacy else {
        comparison.diffs.push(ShadowDiff::LegacyMessageMissing);
        return Ok(comparison);
    };

    if legacy.to_agent != batch.agent_id {
        comparison.diffs.push(ShadowDiff::TargetAgentMismatch);
    }
    if batch.items.len() != 1 {
        comparison.diffs.push(ShadowDiff::BatchShapeMismatch);
    } else if let Some(item) = batch.items.first() {
        if legacy.from_agent != item.from_agent {
            comparison.diffs.push(ShadowDiff::SenderMismatch);
        }
        if legacy.content_digest != digest(&item.content) {
            comparison.diffs.push(ShadowDiff::ContentMismatch);
        }
        let item_summary_digest = item.summary.as_deref().map(digest);
        if legacy.summary_digest != item_summary_digest {
            comparison.diffs.push(ShadowDiff::SummaryMismatch);
        }
    }
    comparison.legacy = Some(legacy);
    Ok(comparison)
}

fn digest(value: &str) -> String {
    format!("sha256:{:x}", Sha256::digest(value.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::guidance_queue::{
        GuidanceBatchRequest, GuidanceIdentity, GuidanceItemInput,
    };
    use serde_json::json;

    fn request() -> GuidanceBatchRequest {
        GuidanceBatchRequest {
            agent_id: "worker".to_string(),
            queue_class: QueueClass::FollowUp,
            items: vec![GuidanceItemInput {
                from_agent: "root".to_string(),
                content: "continue the task".to_string(),
                summary: Some("task guidance".to_string()),
                injection_options: json!({"submit": true}),
            }],
            identity: GuidanceIdentity::default(),
            idempotency_key: Some("shadow-test".to_string()),
            source_message_id: None,
        }
    }

    #[test]
    fn compatibility_projections_start_in_sync() -> Result<()> {
        let store = InboxStore::open_in_memory()?;
        let batch = store.enqueue_batch_with_compatibility(
            request(),
            "root",
            "continue the task",
            Some("task guidance"),
        )?;

        let comparison = compare_batch(&store, &batch.batch.batch_id)?;
        assert!(comparison.in_sync(), "{comparison:?}");
        assert_eq!(
            comparison.legacy.as_ref().map(|legacy| legacy.read),
            Some(false)
        );
        assert_eq!(
            comparison.queue.as_ref().map(|queue| queue.item_count),
            Some(1)
        );
        Ok(())
    }

    #[test]
    fn payload_drift_is_reported_without_exposing_body() -> Result<()> {
        let store = InboxStore::open_in_memory()?;
        let batch = store.enqueue_batch_with_compatibility(
            request(),
            "root",
            "continue the task",
            Some("task guidance"),
        )?;
        store.connection()?.execute(
            "UPDATE guidance_items SET content = 'drifted content' WHERE batch_id = ?1",
            [&batch.batch.batch_id],
        )?;

        let comparison = compare_batch(&store, &batch.batch.batch_id)?;
        assert_eq!(comparison.diffs, vec![ShadowDiff::ContentMismatch]);
        let serialized = serde_json::to_string(&comparison)?;
        assert!(!serialized.contains("continue the task"));
        assert!(!serialized.contains("drifted content"));
        Ok(())
    }

    #[test]
    fn missing_source_projection_is_visible() -> Result<()> {
        let store = InboxStore::open_in_memory()?;
        let batch = store.enqueue_batch(request())?.batch;
        let comparison = compare_batch(&store, &batch.batch_id)?;
        assert_eq!(
            comparison.diffs,
            vec![ShadowDiff::LegacySourcePointerMissing]
        );
        Ok(())
    }
}
