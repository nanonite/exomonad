//! Durable, per-agent steering and follow-up guidance queue.
//!
//! SQLite is the scheduling authority. Transport adapters only report attempts
//! and bounded runtime evidence; neither transport success nor this queue can
//! change review, CI, or merge state.

use super::guidance_adapters::{
    AcceptanceConfidence, BoundaryEvidence, BoundaryPhase, RuntimeAcceptanceEvidence,
    TransportAttempt,
};
use super::inbox_store::{normalize_agent_id, now_epoch_secs, InboxStore};
use super::state_mirror::append_state_change;
use anyhow::{anyhow, bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::Value;

const MAX_BATCH_ITEMS: usize = 32;
const MAX_CONTENT_BYTES: usize = 64 * 1024;
const MAX_SUMMARY_BYTES: usize = 4 * 1024;
const MAX_ID_BYTES: usize = 256;
const MAX_REASON_BYTES: usize = 512;
const MAX_LEASE_SECONDS: u64 = 300;
const MAX_RETRY_ATTEMPTS: i64 = 8;
const MAX_RETRY_BACKOFF_SECONDS: i64 = 300;

/// Logical queue with steering priority at a safe post-turn boundary.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QueueClass {
    Steering,
    FollowUp,
}

impl QueueClass {
    fn as_str(self) -> &'static str {
        match self {
            Self::Steering => "steering",
            Self::FollowUp => "follow_up",
        }
    }

    fn parse(value: &str) -> Result<Self> {
        match value {
            "steering" => Ok(Self::Steering),
            "follow_up" => Ok(Self::FollowUp),
            _ => bail!("unsupported guidance queue class `{value}`"),
        }
    }
}

/// Durable lifecycle state for an atomic guidance batch.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GuidanceState {
    Pending,
    Leased,
    Submitted,
    Accepted,
    Abandoned,
    Cancelled,
}

impl GuidanceState {
    fn as_str(self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Leased => "leased",
            Self::Submitted => "submitted",
            Self::Accepted => "accepted",
            Self::Abandoned => "abandoned",
            Self::Cancelled => "cancelled",
        }
    }

    fn parse(value: &str) -> Result<Self> {
        match value {
            "pending" => Ok(Self::Pending),
            "leased" => Ok(Self::Leased),
            "submitted" => Ok(Self::Submitted),
            "accepted" => Ok(Self::Accepted),
            "abandoned" => Ok(Self::Abandoned),
            "cancelled" => Ok(Self::Cancelled),
            _ => bail!("unsupported guidance state `{value}`"),
        }
    }
}

/// Identity captured when a producer commits a batch.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct GuidanceIdentity {
    pub run_id: Option<String>,
    pub session_id: Option<String>,
    pub invocation_id: Option<String>,
    pub generation: Option<u64>,
    pub runtime: Option<String>,
    pub harness: Option<String>,
    pub role: Option<String>,
}

/// One item in an atomic batch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GuidanceItemInput {
    pub from_agent: String,
    pub content: String,
    pub summary: Option<String>,
    #[serde(default)]
    pub injection_options: Value,
}

/// Request for one durable batch. An idempotency key is scoped to the target.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GuidanceBatchRequest {
    pub agent_id: String,
    pub queue_class: QueueClass,
    pub items: Vec<GuidanceItemInput>,
    #[serde(default)]
    pub identity: GuidanceIdentity,
    pub idempotency_key: Option<String>,
    pub source_message_id: Option<i64>,
}

/// Consumer identity used for the lease and acknowledgement compare-and-set.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GuidanceConsumer {
    pub consumer_id: String,
    pub invocation_id: Option<String>,
    pub generation: Option<u64>,
}

/// Durable item returned with a claimed or inspected batch.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GuidanceItem {
    pub item_id: String,
    pub position: i64,
    pub from_agent: String,
    pub content: String,
    pub summary: Option<String>,
    pub injection_options: Value,
}

/// Complete durable batch and its ordered items.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GuidanceBatch {
    pub batch_id: String,
    pub agent_id: String,
    pub queue_class: QueueClass,
    pub queue_seq: i64,
    pub state: GuidanceState,
    pub created_at: i64,
    pub available_at: i64,
    pub lease_owner: Option<String>,
    pub lease_expires_at: Option<i64>,
    pub attempt_count: i64,
    pub accepted_at: Option<i64>,
    pub terminal_at: Option<i64>,
    pub terminal_reason: Option<String>,
    pub identity: GuidanceIdentity,
    pub source_message_id: Option<i64>,
    pub idempotency_key: Option<String>,
    pub items: Vec<GuidanceItem>,
}

/// Result of an enqueue, including whether an idempotency replay reused a row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GuidanceEnqueueResult {
    pub batch: GuidanceBatch,
    pub created: bool,
}

/// Result of an idempotent runtime acknowledgement.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GuidanceAckResult {
    Accepted,
    AlreadyAccepted,
    Rejected { reason: String },
}

/// Install the durable queue schema without touching legacy inbox tables.
pub(crate) fn migrate(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS guidance_batches (
           batch_id            TEXT PRIMARY KEY,
           agent_id            TEXT NOT NULL,
           queue_class         TEXT NOT NULL CHECK (queue_class IN ('steering', 'follow_up')),
           queue_seq           INTEGER NOT NULL CHECK (queue_seq >= 0),
           state               TEXT NOT NULL CHECK (state IN ('pending', 'leased', 'submitted', 'accepted', 'abandoned', 'cancelled')),
           created_at          INTEGER NOT NULL,
           available_at        INTEGER NOT NULL,
           lease_owner         TEXT,
           lease_expires_at    INTEGER,
           attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
           accepted_at         INTEGER,
           terminal_at         INTEGER,
           terminal_reason     TEXT,
           run_id              TEXT,
           session_id          TEXT,
           invocation_id      TEXT,
           generation          INTEGER,
           runtime             TEXT,
           harness             TEXT,
           role                TEXT,
           source_message_id   INTEGER,
           idempotency_key     TEXT,
           UNIQUE (agent_id, queue_seq),
           UNIQUE (agent_id, idempotency_key)
         );
         CREATE INDEX IF NOT EXISTS idx_guidance_batches_claim
           ON guidance_batches (agent_id, state, available_at, queue_class, queue_seq);
         CREATE TABLE IF NOT EXISTS guidance_items (
           batch_id            TEXT NOT NULL REFERENCES guidance_batches(batch_id) ON DELETE CASCADE,
           position            INTEGER NOT NULL CHECK (position >= 0),
           item_id             TEXT NOT NULL UNIQUE,
           from_agent          TEXT NOT NULL,
           content             TEXT NOT NULL,
           summary             TEXT,
           injection_options   TEXT NOT NULL,
           PRIMARY KEY (batch_id, position)
         );
         CREATE INDEX IF NOT EXISTS idx_guidance_items_batch
           ON guidance_items (batch_id, position);",
    )
    .context("failed to migrate durable guidance queue")?;
    Ok(())
}

impl InboxStore {
    /// Commit one atomic batch before any runtime transport is attempted.
    pub fn enqueue_batch(&self, request: GuidanceBatchRequest) -> Result<GuidanceEnqueueResult> {
        validate_batch_request(&request)?;
        let agent_id = normalize_agent_id(&request.agent_id).into_owned();
        let now = now_epoch_secs();
        let mut conn = self.connection()?;
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .context("failed to start guidance enqueue transaction")?;
        let result = enqueue_batch_in_transaction(&tx, &request, &agent_id, now)?;
        tx.commit().context("failed to commit guidance enqueue")?;
        append_enqueue_event(self, &result);
        Ok(result)
    }

    /// Atomically retain a legacy messages row and link it from the durable
    /// batch before any runtime transport is attempted.
    pub fn enqueue_batch_with_compatibility(
        &self,
        mut request: GuidanceBatchRequest,
        from_agent: &str,
        content: &str,
        summary: Option<&str>,
    ) -> Result<GuidanceEnqueueResult> {
        validate_batch_request(&request)?;
        validate_identity(from_agent, "sender agent")?;
        let agent_id = normalize_agent_id(&request.agent_id).into_owned();
        let now = now_epoch_secs();
        let mut conn = self.connection()?;
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .context("failed to start compatible guidance enqueue transaction")?;

        if let Some(idempotency_key) = request.idempotency_key.as_deref() {
            if let Some(existing) =
                find_idempotent_batch(&tx, &agent_id, idempotency_key, request.queue_class)?
            {
                let batch = load_batch(&tx, &existing)?;
                tx.commit()
                    .context("failed to commit idempotent compatible enqueue")?;
                return Ok(GuidanceEnqueueResult {
                    batch,
                    created: false,
                });
            }
        }

        let normalized_to_agent = normalize_agent_id(&agent_id);
        tx.execute(
            "INSERT INTO messages (from_agent, to_agent, content, summary, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                from_agent,
                normalized_to_agent.as_ref(),
                content,
                summary,
                now
            ],
        )
        .context("failed to insert compatibility inbox message")?;
        let source_message_id = tx.last_insert_rowid();
        request.source_message_id = Some(source_message_id);

        let result = enqueue_batch_in_transaction(&tx, &request, &agent_id, now)?;
        tx.commit()
            .context("failed to commit compatible guidance enqueue")?;
        append_state_change(
            self.db_path(),
            "inbox.state_changed",
            serde_json::json!({
                "operation": "write_message",
                "message_id": source_message_id,
                "from_agent": from_agent,
                "to_agent": normalized_to_agent,
                "content": content,
                "summary": summary,
                "created_at": now
            }),
        );
        append_state_change(
            self.db_path(),
            "message.delivery",
            serde_json::json!({
                "message_id": source_message_id,
                "from_agent": from_agent,
                "to_agent": normalized_to_agent,
                "attempt": 1,
                "outcome": "accepted",
                "transport": "durable_inbox"
            }),
        );
        append_enqueue_event(self, &result);
        Ok(result)
    }

    /// Claim one whole batch only when the adapter reports a safe boundary.
    pub fn claim_next(
        &self,
        boundary: &BoundaryEvidence,
        consumer: &GuidanceConsumer,
        lease_seconds: u64,
    ) -> Result<Option<GuidanceBatch>> {
        validate_identity(&boundary.agent_id, "boundary agent")?;
        validate_id(&consumer.consumer_id, "consumer id")?;
        if !boundary.phase.can_offer_steering() {
            return Ok(None);
        }
        if boundary.phase == BoundaryPhase::WouldStop && !boundary.phase.can_offer_follow_up() {
            return Ok(None);
        }
        let agent_id = normalize_agent_id(&boundary.agent_id).into_owned();
        let lease_seconds = lease_seconds.clamp(1, MAX_LEASE_SECONDS);
        let now = now_epoch_secs();
        let lease_expires_at = now.saturating_add(i64::try_from(lease_seconds).unwrap_or(i64::MAX));
        let mut conn = self.connection()?;
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .context("failed to start guidance claim transaction")?;
        let recovered = recover_expired_in_tx(&tx, now)?;
        let has_active_lease: bool = tx
            .query_row(
                "SELECT EXISTS(
                   SELECT 1 FROM guidance_batches
                   WHERE agent_id = ?1 AND state IN ('leased', 'submitted')
                     AND lease_expires_at IS NOT NULL AND lease_expires_at > ?2
                 )",
                params![&agent_id, now],
                |row| row.get(0),
            )
            .context("failed to inspect active guidance lease")?;
        if has_active_lease {
            tx.commit()
                .context("failed to commit empty guidance claim")?;
            emit_recovery_events(self, recovered);
            return Ok(None);
        }

        if boundary.phase == BoundaryPhase::WouldStop {
            let steering_pending: bool = tx
                .query_row(
                    "SELECT EXISTS(
                       SELECT 1 FROM guidance_batches
                       WHERE agent_id = ?1 AND queue_class = 'steering' AND state = 'pending'
                     )",
                    params![&agent_id],
                    |row| row.get(0),
                )
                .context("failed to inspect pending steering guidance")?;
            let steering_available: bool = tx
                .query_row(
                    "SELECT EXISTS(
                       SELECT 1 FROM guidance_batches
                       WHERE agent_id = ?1 AND queue_class = 'steering'
                         AND state = 'pending' AND available_at <= ?2
                     )",
                    params![&agent_id, now],
                    |row| row.get(0),
                )
                .context("failed to inspect available steering guidance")?;
            if steering_pending && !steering_available {
                tx.commit()
                    .context("failed to commit blocked guidance claim")?;
                emit_recovery_events(self, recovered);
                return Ok(None);
            }
        }

        let class_filter = if boundary.phase == BoundaryPhase::TurnFinished {
            "AND queue_class = 'steering'"
        } else {
            "AND queue_class IN ('steering', 'follow_up')"
        };
        let sql = format!(
            "SELECT batch_id FROM guidance_batches
             WHERE agent_id = ?1 AND state = 'pending' AND available_at <= ?2 {class_filter}
             ORDER BY CASE queue_class WHEN 'steering' THEN 0 ELSE 1 END, queue_seq ASC
             LIMIT 1"
        );
        let batch_id: Option<String> = tx
            .query_row(&sql, params![&agent_id, now], |row| row.get(0))
            .optional()
            .context("failed to select next guidance batch")?;
        let Some(batch_id) = batch_id else {
            tx.commit()
                .context("failed to commit empty guidance claim")?;
            emit_recovery_events(self, recovered);
            return Ok(None);
        };
        let changed = tx
            .execute(
                "UPDATE guidance_batches
                 SET state = 'leased', lease_owner = ?1, lease_expires_at = ?2,
                     invocation_id = COALESCE(?3, invocation_id),
                     generation = COALESCE(?4, generation)
                 WHERE batch_id = ?5 AND state = 'pending' AND available_at <= ?6",
                params![
                    &consumer.consumer_id,
                    lease_expires_at,
                    consumer.invocation_id,
                    consumer.generation,
                    &batch_id,
                    now,
                ],
            )
            .context("failed to lease guidance batch")?;
        if changed != 1 {
            bail!("guidance batch became unavailable during claim")
        }
        let batch = load_batch(&tx, &batch_id)?;
        tx.commit().context("failed to commit guidance claim")?;
        emit_recovery_events(self, recovered);
        append_queue_event(
            self,
            "inbox.state_changed",
            serde_json::json!({
                "operation": "claim",
                "batch_id": batch.batch_id,
                "agent_id": batch.agent_id,
                "queue_class": batch.queue_class.as_str(),
                "queue_seq": batch.queue_seq,
                "consumer": consumer.consumer_id,
                "boundary": boundary.phase,
                "lease_expires_at": batch.lease_expires_at,
            }),
        );
        Ok(Some(batch))
    }

    /// Record one transport attempt without acknowledging runtime consumption.
    pub fn record_transport_attempt(
        &self,
        batch_id: &str,
        consumer_id: &str,
        attempt: &TransportAttempt,
    ) -> Result<i64> {
        validate_id(batch_id, "batch id")?;
        validate_id(consumer_id, "consumer id")?;
        validate_id(&attempt.method, "transport method")?;
        if let Some(detail) = &attempt.detail {
            validate_text(detail, MAX_REASON_BYTES, "transport detail")?;
        }
        let now = now_epoch_secs();
        let mut conn = self.connection()?;
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .context("failed to start guidance transport transaction")?;
        let (state, owner, attempt_count): (String, Option<String>, i64) = tx
            .query_row(
                "SELECT state, lease_owner, attempt_count FROM guidance_batches WHERE batch_id = ?1",
                params![batch_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .context("guidance batch not found")?;
        if state != GuidanceState::Leased.as_str() || owner.as_deref() != Some(consumer_id) {
            bail!("guidance batch is not leased by this consumer")
        }
        let next_attempt = attempt_count
            .checked_add(1)
            .ok_or_else(|| anyhow!("guidance attempt count overflow"))?;
        let lease_expires_at = now.saturating_add(MAX_RETRY_BACKOFF_SECONDS);
        tx.execute(
            "UPDATE guidance_batches
             SET state = 'submitted', attempt_count = ?1, lease_expires_at = ?2
             WHERE batch_id = ?3 AND state = 'leased' AND lease_owner = ?4",
            params![next_attempt, lease_expires_at, batch_id, consumer_id],
        )
        .context("failed to record guidance transport attempt")?;
        tx.commit().context("failed to commit guidance transport")?;
        append_queue_event(
            self,
            "message.delivery",
            serde_json::json!({
                "batch_id": batch_id,
                "consumer": consumer_id,
                "attempt": next_attempt,
                "method": attempt.method,
                "outcome": attempt.outcome,
                "detail": attempt.detail,
            }),
        );
        append_queue_event(
            self,
            "inbox.state_changed",
            serde_json::json!({
                "operation": "transport_submitted",
                "batch_id": batch_id,
                "consumer": consumer_id,
                "attempt": next_attempt,
            }),
        );
        Ok(next_attempt)
    }

    /// Accept the complete batch only with exact, correlated runtime evidence.
    pub fn acknowledge_runtime(
        &self,
        evidence: &RuntimeAcceptanceEvidence,
    ) -> Result<GuidanceAckResult> {
        validate_id(&evidence.batch_id, "batch id")?;
        validate_id(&evidence.agent_id, "evidence agent")?;
        validate_id(&evidence.consumer_id, "consumer id")?;
        let agent_id = normalize_agent_id(&evidence.agent_id).into_owned();
        let now = now_epoch_secs();
        let mut conn = self.connection()?;
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .context("failed to start guidance acknowledgement transaction")?;
        let Some(batch) = load_batch_optional(&tx, &evidence.batch_id)? else {
            return Ok(GuidanceAckResult::Rejected {
                reason: "unknown guidance batch".to_string(),
            });
        };
        if batch.agent_id != agent_id {
            return Ok(GuidanceAckResult::Rejected {
                reason: "guidance target does not match evidence target".to_string(),
            });
        }
        if batch.state == GuidanceState::Accepted {
            tx.commit()
                .context("failed to commit duplicate acknowledgement")?;
            return Ok(if evidence_matches_batch(&batch, evidence) {
                GuidanceAckResult::AlreadyAccepted
            } else {
                GuidanceAckResult::Rejected {
                    reason: "duplicate acknowledgement identity mismatch".to_string(),
                }
            });
        }
        if !matches!(
            batch.state,
            GuidanceState::Leased | GuidanceState::Submitted
        ) {
            tx.commit()
                .context("failed to commit terminal acknowledgement")?;
            return Ok(GuidanceAckResult::Rejected {
                reason: format!("guidance batch is {}", batch.state.as_str()),
            });
        }
        if !evidence_matches_batch(&batch, evidence) {
            tx.commit()
                .context("failed to commit rejected acknowledgement")?;
            return Ok(GuidanceAckResult::Rejected {
                reason: "acknowledgement identity does not match claimed batch".to_string(),
            });
        }
        if evidence.confidence != AcceptanceConfidence::Exact {
            tx.commit()
                .context("failed to commit unproven acknowledgement")?;
            return Ok(GuidanceAckResult::Rejected {
                reason: "non-exact runtime evidence cannot accept a guidance batch".to_string(),
            });
        }
        let changed = tx
            .execute(
                "UPDATE guidance_batches
                 SET state = 'accepted', accepted_at = ?1, lease_owner = NULL,
                     lease_expires_at = NULL
                 WHERE batch_id = ?2 AND state IN ('leased', 'submitted')
                   AND lease_owner = ?3",
                params![now, evidence.batch_id, evidence.consumer_id],
            )
            .context("failed to accept guidance batch")?;
        if changed != 1 {
            tx.commit()
                .context("failed to commit concurrent acknowledgement")?;
            return Ok(GuidanceAckResult::Rejected {
                reason: "guidance batch changed before acknowledgement".to_string(),
            });
        }
        tx.commit()
            .context("failed to commit guidance acknowledgement")?;
        append_queue_event(
            self,
            "inbox.state_changed",
            serde_json::json!({
                "operation": "acknowledge",
                "batch_id": evidence.batch_id,
                "agent_id": agent_id,
                "consumer": evidence.consumer_id,
                "evidence_kind": evidence.evidence_kind,
                "confidence": evidence.confidence,
            }),
        );
        append_queue_event(
            self,
            "message.consumed",
            serde_json::json!({
                "batch_id": evidence.batch_id,
                "agent_id": agent_id,
                "consumer": evidence.consumer_id,
                "ack_kind": format!("{:?}", evidence.evidence_kind).to_lowercase(),
                "confidence": evidence.confidence,
                "item_count": evidence.item_ids.len(),
            }),
        );
        Ok(GuidanceAckResult::Accepted)
    }

    /// Return a leased or submitted batch to pending, or abandon it at budget.
    pub fn release_for_retry(
        &self,
        batch_id: &str,
        consumer_id: &str,
        reason: &str,
        next_attempt_at: i64,
    ) -> Result<GuidanceState> {
        validate_id(batch_id, "batch id")?;
        validate_id(consumer_id, "consumer id")?;
        validate_text(reason, MAX_REASON_BYTES, "retry reason")?;
        let now = now_epoch_secs();
        let next_attempt_at = next_attempt_at.max(now);
        let mut conn = self.connection()?;
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .context("failed to start guidance retry transaction")?;
        let (state, owner, attempt_count): (String, Option<String>, i64) = tx
            .query_row(
                "SELECT state, lease_owner, attempt_count FROM guidance_batches WHERE batch_id = ?1",
                params![batch_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .context("guidance batch not found")?;
        if !matches!(state.as_str(), "leased" | "submitted")
            || owner.as_deref() != Some(consumer_id)
        {
            bail!("guidance batch is not retryable for this consumer")
        }
        let terminal = attempt_count >= MAX_RETRY_ATTEMPTS;
        let new_state = if terminal {
            GuidanceState::Abandoned
        } else {
            GuidanceState::Pending
        };
        tx.execute(
            "UPDATE guidance_batches
             SET state = ?1, available_at = ?2, lease_owner = NULL,
                 lease_expires_at = NULL, terminal_at = ?3, terminal_reason = ?4
             WHERE batch_id = ?5 AND state IN ('leased', 'submitted') AND lease_owner = ?6",
            params![
                new_state.as_str(),
                next_attempt_at,
                terminal.then_some(now),
                terminal.then_some(reason),
                batch_id,
                consumer_id,
            ],
        )
        .context("failed to release guidance batch")?;
        tx.commit().context("failed to commit guidance retry")?;
        append_queue_event(
            self,
            "inbox.state_changed",
            serde_json::json!({
                "operation": if terminal { "abandon" } else { "release_for_retry" },
                "batch_id": batch_id,
                "consumer": consumer_id,
                "state": new_state,
                "reason": reason,
                "available_at": next_attempt_at,
            }),
        );
        if terminal {
            append_queue_event(
                self,
                "agent_inbox.messages_abandoned",
                serde_json::json!({
                    "batch_id": batch_id,
                    "consumer": consumer_id,
                    "reason": reason,
                    "attempt": attempt_count,
                }),
            );
        }
        Ok(new_state)
    }

    /// Explicitly cancel any non-terminal batch. Accepted batches are stable.
    pub fn cancel_batch(&self, batch_id: &str, reason: &str) -> Result<GuidanceState> {
        validate_id(batch_id, "batch id")?;
        validate_text(reason, MAX_REASON_BYTES, "cancel reason")?;
        let now = now_epoch_secs();
        let mut conn = self.connection()?;
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .context("failed to start guidance cancellation transaction")?;
        let state: String = tx
            .query_row(
                "SELECT state FROM guidance_batches WHERE batch_id = ?1",
                params![batch_id],
                |row| row.get(0),
            )
            .context("guidance batch not found")?;
        let current = GuidanceState::parse(&state)?;
        if matches!(
            current,
            GuidanceState::Accepted | GuidanceState::Abandoned | GuidanceState::Cancelled
        ) {
            tx.commit()
                .context("failed to commit idempotent cancellation")?;
            return Ok(current);
        }
        tx.execute(
            "UPDATE guidance_batches
             SET state = 'cancelled', lease_owner = NULL, lease_expires_at = NULL,
                 terminal_at = ?1, terminal_reason = ?2
             WHERE batch_id = ?3 AND state IN ('pending', 'leased', 'submitted')",
            params![now, reason, batch_id],
        )
        .context("failed to cancel guidance batch")?;
        tx.commit()
            .context("failed to commit guidance cancellation")?;
        append_queue_event(
            self,
            "inbox.state_changed",
            serde_json::json!({
                "operation": "cancel",
                "batch_id": batch_id,
                "state": GuidanceState::Cancelled,
                "reason": reason,
            }),
        );
        Ok(GuidanceState::Cancelled)
    }

    /// Recover expired leases after restart or before a new claim.
    pub fn recover_expired_leases(&self, now: i64) -> Result<Vec<String>> {
        let mut conn = self.connection()?;
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .context("failed to start guidance recovery transaction")?;
        let recovered = recover_expired_in_tx(&tx, now)?;
        tx.commit().context("failed to commit guidance recovery")?;
        emit_recovery_events(self, recovered.clone());
        Ok(recovered
            .into_iter()
            .map(|(batch_id, _)| batch_id)
            .collect())
    }
}

fn enqueue_batch_in_transaction(
    tx: &Transaction<'_>,
    request: &GuidanceBatchRequest,
    agent_id: &str,
    now: i64,
) -> Result<GuidanceEnqueueResult> {
    if let Some(idempotency_key) = request.idempotency_key.as_deref() {
        if let Some(existing) =
            find_idempotent_batch(tx, agent_id, idempotency_key, request.queue_class)?
        {
            let batch = load_batch(tx, &existing)?;
            return Ok(GuidanceEnqueueResult {
                batch,
                created: false,
            });
        }
    }

    let queue_seq: i64 = tx
        .query_row(
            "SELECT COALESCE(MAX(queue_seq), -1) + 1 FROM guidance_batches WHERE agent_id = ?1",
            params![agent_id],
            |row| row.get(0),
        )
        .context("failed to allocate guidance queue sequence")?;
    let batch_id = uuid::Uuid::new_v4().to_string();
    tx.execute(
        "INSERT INTO guidance_batches (
           batch_id, agent_id, queue_class, queue_seq, state, created_at,
           available_at, attempt_count, run_id, session_id, invocation_id,
           generation, runtime, harness, role, source_message_id, idempotency_key
         ) VALUES (?1, ?2, ?3, ?4, 'pending', ?5, ?5, 0, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
        params![
            &batch_id,
            agent_id,
            request.queue_class.as_str(),
            queue_seq,
            now,
            request.identity.run_id.as_deref(),
            request.identity.session_id.as_deref(),
            request.identity.invocation_id.as_deref(),
            request.identity.generation,
            request.identity.runtime.as_deref(),
            request.identity.harness.as_deref(),
            request.identity.role.as_deref(),
            request.source_message_id,
            request.idempotency_key.as_deref(),
        ],
    )
    .context("failed to insert guidance batch")?;
    for (position, item) in request.items.iter().enumerate() {
        let item_id = uuid::Uuid::new_v4().to_string();
        let injection_options = serde_json::to_string(&item.injection_options)
            .context("failed to encode guidance injection options")?;
        tx.execute(
            "INSERT INTO guidance_items (
               batch_id, position, item_id, from_agent, content, summary, injection_options
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                &batch_id,
                i64::try_from(position).context("guidance item position overflow")?,
                item_id,
                &item.from_agent,
                &item.content,
                &item.summary,
                injection_options,
            ],
        )
        .context("failed to insert guidance item")?;
    }
    Ok(GuidanceEnqueueResult {
        batch: load_batch(tx, &batch_id)?,
        created: true,
    })
}

fn append_enqueue_event(store: &InboxStore, result: &GuidanceEnqueueResult) {
    if !result.created {
        return;
    }
    let batch = &result.batch;
    append_queue_event(
        store,
        "inbox.state_changed",
        serde_json::json!({
            "operation": "enqueue",
            "batch_id": batch.batch_id,
            "agent_id": batch.agent_id,
            "queue_class": batch.queue_class.as_str(),
            "queue_seq": batch.queue_seq,
            "item_count": batch.items.len(),
            "idempotency_key": batch.idempotency_key,
        }),
    );
}

fn validate_batch_request(request: &GuidanceBatchRequest) -> Result<()> {
    validate_identity(&request.agent_id, "target agent")?;
    if request.items.is_empty() || request.items.len() > MAX_BATCH_ITEMS {
        bail!("guidance batch must contain between 1 and {MAX_BATCH_ITEMS} items")
    }
    if let Some(key) = &request.idempotency_key {
        validate_id(key, "idempotency key")?;
    }
    for item in &request.items {
        validate_identity(&item.from_agent, "sender agent")?;
        validate_text(&item.content, MAX_CONTENT_BYTES, "guidance content")?;
        if let Some(summary) = &item.summary {
            validate_text(summary, MAX_SUMMARY_BYTES, "guidance summary")?;
        }
    }
    validate_identity_fields(&request.identity)
}

fn validate_identity_fields(identity: &GuidanceIdentity) -> Result<()> {
    for (name, value) in [
        ("run id", identity.run_id.as_deref()),
        ("session id", identity.session_id.as_deref()),
        ("invocation id", identity.invocation_id.as_deref()),
        ("runtime", identity.runtime.as_deref()),
        ("harness", identity.harness.as_deref()),
        ("role", identity.role.as_deref()),
    ] {
        if let Some(value) = value {
            validate_id(value, name)?;
        }
    }
    Ok(())
}

fn validate_identity(value: &str, field: &str) -> Result<()> {
    let normalized = normalize_agent_id(value);
    if normalized.trim().is_empty() {
        bail!("{field} must not be empty")
    }
    Ok(())
}

fn validate_id(value: &str, field: &str) -> Result<()> {
    if value.trim().is_empty() || value.len() > MAX_ID_BYTES {
        bail!("{field} must be non-empty and at most {MAX_ID_BYTES} bytes")
    }
    Ok(())
}

fn validate_text(value: &str, max_bytes: usize, field: &str) -> Result<()> {
    if value.is_empty() || value.len() > max_bytes {
        bail!("{field} must be non-empty and at most {max_bytes} bytes")
    }
    Ok(())
}

fn find_idempotent_batch(
    tx: &Transaction<'_>,
    agent_id: &str,
    idempotency_key: &str,
    queue_class: QueueClass,
) -> Result<Option<String>> {
    let existing: Option<(String, String)> = tx
        .query_row(
            "SELECT batch_id, queue_class FROM guidance_batches
             WHERE agent_id = ?1 AND idempotency_key = ?2",
            params![agent_id, idempotency_key],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .context("failed to inspect guidance idempotency key")?;
    let Some((batch_id, existing_class)) = existing else {
        return Ok(None);
    };
    if existing_class != queue_class.as_str() {
        bail!("idempotency key already belongs to another queue class")
    }
    Ok(Some(batch_id))
}

fn load_batch_optional(conn: &Connection, batch_id: &str) -> Result<Option<GuidanceBatch>> {
    let exists: Option<String> = conn
        .query_row(
            "SELECT batch_id FROM guidance_batches WHERE batch_id = ?1",
            params![batch_id],
            |row| row.get(0),
        )
        .optional()
        .context("failed to find guidance batch")?;
    exists.map(|id| load_batch(conn, &id)).transpose()
}

fn load_batch(conn: &Connection, batch_id: &str) -> Result<GuidanceBatch> {
    let row = conn
        .query_row(
            "SELECT batch_id, agent_id, queue_class, queue_seq, state, created_at,
                    available_at, lease_owner, lease_expires_at, attempt_count,
                    accepted_at, terminal_at, terminal_reason, run_id, session_id,
                    invocation_id, generation, runtime, harness, role,
                    source_message_id, idempotency_key
             FROM guidance_batches WHERE batch_id = ?1",
            params![batch_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, i64>(5)?,
                    row.get::<_, i64>(6)?,
                    row.get::<_, Option<String>>(7)?,
                    row.get::<_, Option<i64>>(8)?,
                    row.get::<_, i64>(9)?,
                    row.get::<_, Option<i64>>(10)?,
                    row.get::<_, Option<i64>>(11)?,
                    row.get::<_, Option<String>>(12)?,
                    row.get::<_, Option<String>>(13)?,
                    row.get::<_, Option<String>>(14)?,
                    row.get::<_, Option<String>>(15)?,
                    row.get::<_, Option<u64>>(16)?,
                    row.get::<_, Option<String>>(17)?,
                    row.get::<_, Option<String>>(18)?,
                    row.get::<_, Option<String>>(19)?,
                    row.get::<_, Option<i64>>(20)?,
                    row.get::<_, Option<String>>(21)?,
                ))
            },
        )
        .context("failed to load guidance batch")?;
    let mut item_stmt = conn
        .prepare(
            "SELECT item_id, position, from_agent, content, summary, injection_options
             FROM guidance_items WHERE batch_id = ?1 ORDER BY position ASC",
        )
        .context("failed to prepare guidance item query")?;
    let items = item_stmt
        .query_map(params![batch_id], |row| {
            let options: String = row.get(5)?;
            let injection_options = serde_json::from_str(&options).map_err(|error| {
                rusqlite::Error::FromSqlConversionFailure(
                    5,
                    rusqlite::types::Type::Text,
                    Box::new(error),
                )
            })?;
            Ok(GuidanceItem {
                item_id: row.get(0)?,
                position: row.get(1)?,
                from_agent: row.get(2)?,
                content: row.get(3)?,
                summary: row.get(4)?,
                injection_options,
            })
        })
        .context("failed to query guidance items")?
        .collect::<rusqlite::Result<Vec<_>>>()
        .context("failed to decode guidance items")?;
    validate_item_positions(&items)?;
    Ok(GuidanceBatch {
        batch_id: row.0,
        agent_id: row.1,
        queue_class: QueueClass::parse(&row.2)?,
        queue_seq: row.3,
        state: GuidanceState::parse(&row.4)?,
        created_at: row.5,
        available_at: row.6,
        lease_owner: row.7,
        lease_expires_at: row.8,
        attempt_count: row.9,
        accepted_at: row.10,
        terminal_at: row.11,
        terminal_reason: row.12,
        identity: GuidanceIdentity {
            run_id: row.13,
            session_id: row.14,
            invocation_id: row.15,
            generation: row.16,
            runtime: row.17,
            harness: row.18,
            role: row.19,
        },
        source_message_id: row.20,
        idempotency_key: row.21,
        items,
    })
}

fn validate_item_positions(items: &[GuidanceItem]) -> Result<()> {
    for (expected, item) in items.iter().enumerate() {
        if item.position != i64::try_from(expected).unwrap_or(i64::MAX) {
            bail!("guidance item positions must be contiguous from zero")
        }
    }
    Ok(())
}

fn evidence_matches_batch(batch: &GuidanceBatch, evidence: &RuntimeAcceptanceEvidence) -> bool {
    let item_ids = batch
        .items
        .iter()
        .map(|item| item.item_id.as_str())
        .collect::<Vec<_>>();
    let evidence_ids = evidence
        .item_ids
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>();
    batch.agent_id == normalize_agent_id(&evidence.agent_id)
        && batch.queue_class.as_str() == evidence.queue_class
        && batch
            .lease_owner
            .as_deref()
            .is_none_or(|owner| owner == evidence.consumer_id)
        && batch.identity.invocation_id == evidence.invocation_id
        && batch.identity.generation == evidence.generation
        && item_ids == evidence_ids
}

fn recover_expired_in_tx(tx: &Transaction<'_>, now: i64) -> Result<Vec<(String, GuidanceState)>> {
    let mut stmt = tx
        .prepare(
            "SELECT batch_id, attempt_count FROM guidance_batches
             WHERE state IN ('leased', 'submitted')
               AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?1",
        )
        .context("failed to prepare expired guidance query")?;
    let rows = stmt
        .query_map(params![now], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()
        .context("failed to decode expired guidance rows")?;
    drop(stmt);
    let mut recovered = Vec::with_capacity(rows.len());
    for (batch_id, attempt_count) in rows {
        let abandon = attempt_count >= MAX_RETRY_ATTEMPTS;
        let state = if abandon {
            GuidanceState::Abandoned
        } else {
            GuidanceState::Pending
        };
        let available_at = if abandon {
            now
        } else {
            now.saturating_add(retry_backoff_seconds(attempt_count))
        };
        tx.execute(
            "UPDATE guidance_batches
             SET state = ?1, available_at = ?2, lease_owner = NULL,
                 lease_expires_at = NULL, terminal_at = ?3,
                 terminal_reason = ?4
             WHERE batch_id = ?5 AND state IN ('leased', 'submitted')
               AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?6",
            params![
                state.as_str(),
                available_at,
                abandon.then_some(now),
                abandon.then_some("lease_expired_retry_budget"),
                batch_id,
                now,
            ],
        )
        .context("failed to recover expired guidance batch")?;
        recovered.push((batch_id, state));
    }
    Ok(recovered)
}

fn retry_backoff_seconds(attempt_count: i64) -> i64 {
    let exponent = u32::try_from(attempt_count.clamp(0, 8)).unwrap_or(8);
    1_i64
        .checked_shl(exponent)
        .unwrap_or(MAX_RETRY_BACKOFF_SECONDS)
        .min(MAX_RETRY_BACKOFF_SECONDS)
}

fn emit_recovery_events(store: &InboxStore, recovered: Vec<(String, GuidanceState)>) {
    for (batch_id, state) in recovered {
        append_queue_event(
            store,
            "inbox.state_changed",
            serde_json::json!({
                "operation": "lease_expired",
                "batch_id": batch_id,
                "state": state,
            }),
        );
        if state == GuidanceState::Abandoned {
            append_queue_event(
                store,
                "agent_inbox.messages_abandoned",
                serde_json::json!({
                    "batch_id": batch_id,
                    "reason": "lease_expired_retry_budget",
                }),
            );
        }
    }
}

fn append_queue_event(store: &InboxStore, event_type: &str, data: Value) {
    append_state_change(store.db_path(), event_type, data);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::guidance_adapters::{
        AcceptanceKind, BoundaryEvidence, RuntimeAcceptanceEvidence,
    };
    use serde_json::json;

    fn request(agent_id: &str, queue_class: QueueClass, key: Option<&str>) -> GuidanceBatchRequest {
        GuidanceBatchRequest {
            agent_id: agent_id.to_string(),
            queue_class,
            items: vec![GuidanceItemInput {
                from_agent: "root".to_string(),
                content: "continue the assigned task".to_string(),
                summary: Some("task guidance".to_string()),
                injection_options: json!({"submit": true}),
            }],
            identity: GuidanceIdentity {
                invocation_id: Some("invocation-a".to_string()),
                generation: Some(1),
                ..GuidanceIdentity::default()
            },
            idempotency_key: key.map(str::to_string),
            source_message_id: None,
        }
    }

    fn ack(batch: &GuidanceBatch, consumer_id: &str) -> RuntimeAcceptanceEvidence {
        RuntimeAcceptanceEvidence {
            batch_id: batch.batch_id.clone(),
            agent_id: batch.agent_id.clone(),
            queue_class: batch.queue_class.as_str().to_string(),
            item_ids: batch
                .items
                .iter()
                .map(|item| item.item_id.clone())
                .collect(),
            consumer_id: consumer_id.to_string(),
            invocation_id: batch.identity.invocation_id.clone(),
            generation: batch.identity.generation,
            evidence_kind: AcceptanceKind::RuntimeBoundary,
            confidence: AcceptanceConfidence::Exact,
            correlation_id: Some("boundary-1".to_string()),
        }
    }

    #[test]
    fn enqueue_is_atomic_and_idempotent() -> Result<()> {
        let store = InboxStore::open_in_memory()?;
        let first =
            store.enqueue_batch(request("main.agent", QueueClass::Steering, Some("event-1")))?;
        assert!(first.created);
        assert_eq!(first.batch.agent_id, "agent");
        assert_eq!(first.batch.queue_seq, 0);
        assert_eq!(first.batch.items[0].position, 0);
        let replay =
            store.enqueue_batch(request("agent", QueueClass::Steering, Some("event-1")))?;
        assert!(!replay.created);
        assert_eq!(replay.batch.batch_id, first.batch.batch_id);
        Ok(())
    }

    #[test]
    fn claim_prioritizes_steering_and_blocks_second_lease() -> Result<()> {
        let store = InboxStore::open_in_memory()?;
        store.enqueue_batch(request("agent", QueueClass::FollowUp, Some("follow-up")))?;
        store.enqueue_batch(request("agent", QueueClass::Steering, Some("steering")))?;
        let boundary = BoundaryEvidence::would_stop("agent");
        let consumer = GuidanceConsumer {
            consumer_id: "consumer-a".to_string(),
            invocation_id: Some("invocation-a".to_string()),
            generation: Some(1),
        };
        let claimed = store.claim_next(&boundary, &consumer, 60)?.expect("claim");
        assert_eq!(claimed.queue_class, QueueClass::Steering);
        assert!(store.claim_next(&boundary, &consumer, 60)?.is_none());
        Ok(())
    }

    #[test]
    fn pending_steering_does_not_allow_follow_up_to_leapfrog() -> Result<()> {
        let store = InboxStore::open_in_memory()?;
        let steering = store
            .enqueue_batch(request("agent", QueueClass::Steering, Some("steering")))?
            .batch;
        store.enqueue_batch(request("agent", QueueClass::FollowUp, Some("follow-up")))?;
        store.connection()?.execute(
            "UPDATE guidance_batches SET available_at = ?1 WHERE batch_id = ?2",
            params![now_epoch_secs() + 60, steering.batch_id],
        )?;
        let claimed = store.claim_next(
            &BoundaryEvidence::would_stop("agent"),
            &GuidanceConsumer {
                consumer_id: "consumer-a".to_string(),
                invocation_id: Some("invocation-a".to_string()),
                generation: Some(1),
            },
            60,
        )?;
        assert!(claimed.is_none());
        Ok(())
    }

    #[test]
    fn concurrent_claims_exclude_one_agent_but_independent_agents_progress() -> Result<()> {
        use std::sync::{Arc, Barrier};
        use tempfile::TempDir;

        let directory = TempDir::new()?;
        let first_store = Arc::new(InboxStore::open(directory.path())?);
        let second_store = Arc::new(InboxStore::open(directory.path())?);
        first_store.enqueue_batch(request("agent-a", QueueClass::Steering, Some("a")))?;
        first_store.enqueue_batch(request("agent-b", QueueClass::Steering, Some("b")))?;
        let barrier = Arc::new(Barrier::new(2));
        let first_barrier = Arc::clone(&barrier);
        let second_barrier = Arc::clone(&barrier);
        let first = Arc::clone(&first_store);
        let second = Arc::clone(&second_store);
        let first_thread = std::thread::spawn(move || {
            first_barrier.wait();
            first.claim_next(
                &BoundaryEvidence::turn_finished("agent-a"),
                &GuidanceConsumer {
                    consumer_id: "consumer-a".to_string(),
                    invocation_id: Some("invocation-a".to_string()),
                    generation: Some(1),
                },
                60,
            )
        });
        let second_thread = std::thread::spawn(move || {
            second_barrier.wait();
            second.claim_next(
                &BoundaryEvidence::turn_finished("agent-a"),
                &GuidanceConsumer {
                    consumer_id: "consumer-b".to_string(),
                    invocation_id: Some("invocation-a".to_string()),
                    generation: Some(1),
                },
                60,
            )
        });
        let first_claim = first_thread.join().expect("first claim thread")?;
        let second_claim = second_thread.join().expect("second claim thread")?;
        assert_eq!(
            first_claim.is_some() as u8 + second_claim.is_some() as u8,
            1
        );

        let independent = first_store.claim_next(
            &BoundaryEvidence::turn_finished("agent-b"),
            &GuidanceConsumer {
                consumer_id: "consumer-c".to_string(),
                invocation_id: Some("invocation-a".to_string()),
                generation: Some(1),
            },
            60,
        )?;
        assert!(independent.is_some());
        Ok(())
    }

    #[test]
    fn concurrent_claims_allow_independent_agents_to_progress() -> Result<()> {
        use std::sync::{Arc, Barrier};
        use tempfile::TempDir;

        let directory = TempDir::new()?;
        let first_store = Arc::new(InboxStore::open(directory.path())?);
        let second_store = Arc::new(InboxStore::open(directory.path())?);
        let first_batch = first_store
            .enqueue_batch(request("agent-a", QueueClass::Steering, Some("a")))?
            .batch;
        let second_batch = first_store
            .enqueue_batch(request("agent-b", QueueClass::Steering, Some("b")))?
            .batch;
        let barrier = Arc::new(Barrier::new(2));
        let first_barrier = Arc::clone(&barrier);
        let second_barrier = Arc::clone(&barrier);
        let first = Arc::clone(&first_store);
        let second = Arc::clone(&second_store);
        let first_thread = std::thread::spawn(move || {
            first_barrier.wait();
            first.claim_next(
                &BoundaryEvidence::turn_finished("agent-a"),
                &GuidanceConsumer {
                    consumer_id: "consumer-a".to_string(),
                    invocation_id: Some("invocation-a".to_string()),
                    generation: Some(1),
                },
                60,
            )
        });
        let second_thread = std::thread::spawn(move || {
            second_barrier.wait();
            second.claim_next(
                &BoundaryEvidence::turn_finished("agent-b"),
                &GuidanceConsumer {
                    consumer_id: "consumer-b".to_string(),
                    invocation_id: Some("invocation-a".to_string()),
                    generation: Some(1),
                },
                60,
            )
        });
        let first_claim = first_thread
            .join()
            .expect("first claim thread")?
            .expect("claim");
        let second_claim = second_thread
            .join()
            .expect("second claim thread")?
            .expect("claim");
        assert_eq!(first_claim.batch_id, first_batch.batch_id);
        assert_eq!(second_claim.batch_id, second_batch.batch_id);
        Ok(())
    }

    #[test]
    fn invalid_enqueue_is_atomic_and_queue_limits_are_enforced() -> Result<()> {
        let store = InboxStore::open_in_memory()?;
        let mut invalid = request("agent", QueueClass::Steering, None);
        invalid.items[0].from_agent = " ".to_string();
        assert!(store.enqueue_batch(invalid).is_err());

        let mut too_many = request("agent", QueueClass::Steering, None);
        too_many.items = (0..=MAX_BATCH_ITEMS)
            .map(|position| GuidanceItemInput {
                from_agent: "root".to_string(),
                content: format!("item-{position}"),
                summary: None,
                injection_options: Value::Null,
            })
            .collect();
        assert!(store.enqueue_batch(too_many).is_err());

        let batch_count: i64 =
            store
                .connection()?
                .query_row("SELECT COUNT(*) FROM guidance_batches", [], |row| {
                    row.get(0)
                })?;
        let item_count: i64 =
            store
                .connection()?
                .query_row("SELECT COUNT(*) FROM guidance_items", [], |row| row.get(0))?;
        assert_eq!(batch_count, 0);
        assert_eq!(item_count, 0);
        Ok(())
    }

    #[test]
    fn leased_batch_recovers_after_store_restart() -> Result<()> {
        use tempfile::TempDir;

        let directory = TempDir::new()?;
        let batch_id = {
            let store = InboxStore::open(directory.path())?;
            let batch = store
                .enqueue_batch(request("agent", QueueClass::Steering, None))?
                .batch;
            store
                .claim_next(
                    &BoundaryEvidence::turn_finished("agent"),
                    &GuidanceConsumer {
                        consumer_id: "consumer-a".to_string(),
                        invocation_id: Some("invocation-a".to_string()),
                        generation: Some(1),
                    },
                    1,
                )?
                .expect("leased batch");
            batch.batch_id
        };

        let store = InboxStore::open(directory.path())?;
        let recovery_now = now_epoch_secs() + MAX_RETRY_BACKOFF_SECONDS + 2;
        assert_eq!(
            store.recover_expired_leases(recovery_now)?,
            vec![batch_id.clone()]
        );
        store.connection()?.execute(
            "UPDATE guidance_batches SET available_at = ?1 WHERE batch_id = ?2",
            params![now_epoch_secs(), batch_id],
        )?;
        let reclaimed = store
            .claim_next(
                &BoundaryEvidence::turn_finished("agent"),
                &GuidanceConsumer {
                    consumer_id: "consumer-b".to_string(),
                    invocation_id: Some("invocation-a".to_string()),
                    generation: Some(1),
                },
                60,
            )?
            .expect("reclaimed batch");
        assert_eq!(reclaimed.batch_id, batch_id);
        assert_eq!(reclaimed.state, GuidanceState::Leased);
        Ok(())
    }

    #[test]
    fn retry_preserves_sequence_and_abandonment_is_terminal() -> Result<()> {
        let store = InboxStore::open_in_memory()?;
        let first = store
            .enqueue_batch(request("agent", QueueClass::Steering, Some("first")))?
            .batch;
        let second = store
            .enqueue_batch(request("agent", QueueClass::Steering, Some("second")))?
            .batch;
        assert_eq!(first.queue_seq, 0);
        assert_eq!(second.queue_seq, 1);

        let consumer = GuidanceConsumer {
            consumer_id: "consumer-a".to_string(),
            invocation_id: Some("invocation-a".to_string()),
            generation: Some(1),
        };
        let claimed = store
            .claim_next(&BoundaryEvidence::turn_finished("agent"), &consumer, 60)?
            .expect("claim");
        store.release_for_retry(
            &claimed.batch_id,
            &consumer.consumer_id,
            "transient transport failure",
            now_epoch_secs(),
        )?;
        let retried = store
            .claim_next(&BoundaryEvidence::turn_finished("agent"), &consumer, 60)?
            .expect("retry claim");
        assert_eq!(retried.batch_id, first.batch_id);
        assert_eq!(retried.queue_seq, first.queue_seq);

        store.connection()?.execute(
            "UPDATE guidance_batches SET attempt_count = ?1 WHERE batch_id = ?2",
            params![MAX_RETRY_ATTEMPTS, first.batch_id],
        )?;
        assert_eq!(
            store.release_for_retry(
                &first.batch_id,
                &consumer.consumer_id,
                "retry budget exhausted",
                now_epoch_secs(),
            )?,
            GuidanceState::Abandoned
        );
        let next = store
            .claim_next(&BoundaryEvidence::turn_finished("agent"), &consumer, 60)?
            .expect("next batch after abandonment");
        assert_eq!(next.batch_id, second.batch_id);
        assert_eq!(next.queue_seq, second.queue_seq);
        Ok(())
    }

    #[test]
    fn transport_success_does_not_accept_and_exact_ack_is_atomic() -> Result<()> {
        let store = InboxStore::open_in_memory()?;
        let batch = store
            .enqueue_batch(request("agent", QueueClass::Steering, None))?
            .batch;
        let consumer = GuidanceConsumer {
            consumer_id: "consumer-a".to_string(),
            invocation_id: Some("invocation-a".to_string()),
            generation: Some(1),
        };
        let claimed = store
            .claim_next(&BoundaryEvidence::turn_finished("agent"), &consumer, 60)?
            .expect("claim");
        store.record_transport_attempt(
            &batch.batch_id,
            "consumer-a",
            &TransportAttempt::success("tmux"),
        )?;
        let rejected = store.acknowledge_runtime(&RuntimeAcceptanceEvidence {
            confidence: AcceptanceConfidence::Unknown,
            ..ack(&claimed, "consumer-a")
        })?;
        assert!(matches!(rejected, GuidanceAckResult::Rejected { .. }));
        let accepted = store.acknowledge_runtime(&ack(&claimed, "consumer-a"))?;
        assert_eq!(accepted, GuidanceAckResult::Accepted);
        let duplicate = store.acknowledge_runtime(&ack(&claimed, "consumer-a"))?;
        assert_eq!(duplicate, GuidanceAckResult::AlreadyAccepted);
        Ok(())
    }

    #[test]
    fn expired_submitted_batch_returns_to_pending() -> Result<()> {
        let store = InboxStore::open_in_memory()?;
        let batch = store
            .enqueue_batch(request("agent", QueueClass::Steering, None))?
            .batch;
        let consumer = GuidanceConsumer {
            consumer_id: "consumer-a".to_string(),
            invocation_id: Some("invocation-a".to_string()),
            generation: Some(1),
        };
        let claimed = store
            .claim_next(&BoundaryEvidence::turn_finished("agent"), &consumer, 1)?
            .expect("claim");
        store.record_transport_attempt(
            &claimed.batch_id,
            "consumer-a",
            &TransportAttempt::success("uds"),
        )?;
        let recovered =
            store.recover_expired_leases(now_epoch_secs() + MAX_RETRY_BACKOFF_SECONDS + 2)?;
        assert_eq!(recovered, vec![batch.batch_id]);
        Ok(())
    }

    #[test]
    fn cancel_is_terminal_and_not_claimable() -> Result<()> {
        let store = InboxStore::open_in_memory()?;
        let batch = store
            .enqueue_batch(request("agent", QueueClass::Steering, Some("cancelled")))?
            .batch;
        assert_eq!(batch.queue_seq, 0);
        assert_eq!(
            store.cancel_batch(&batch.batch_id, "operator_stop")?,
            GuidanceState::Cancelled
        );
        let next = store
            .enqueue_batch(request("agent", QueueClass::Steering, Some("after-cancel")))?
            .batch;
        assert_eq!(next.queue_seq, 1);
        let claimed = store
            .claim_next(
                &BoundaryEvidence::turn_finished("agent"),
                &GuidanceConsumer {
                    consumer_id: "consumer-a".to_string(),
                    invocation_id: None,
                    generation: None,
                },
                60,
            )?
            .expect("post-cancel batch");
        assert_eq!(claimed.batch_id, next.batch_id);
        assert_eq!(claimed.queue_seq, 1);
        Ok(())
    }
}
