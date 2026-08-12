use super::guidance_queue::GuidanceBatch;
use super::inbox_store::{now_epoch_secs, InboxStore};
use super::tmux_events::InjectionOptions;
use anyhow::{anyhow, Context, Result};
use std::collections::{HashMap, HashSet, VecDeque};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::LazyLock;
use tokio::sync::Mutex;

const DEFAULT_WARNING_THRESHOLD: usize = 8;
const DEFAULT_HARD_CAP: usize = 32;

pub static GLOBAL_AGENT_INBOX: LazyLock<AgentInbox> = LazyLock::new(AgentInbox::default);

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct DedupKey {
    recipient: String,
    event_type: String,
    scope_key: Option<u64>,
    idempotency_key: String,
}

impl DedupKey {
    fn structured(recipient: &str, event_type: &str, scope_key: Option<u64>) -> Self {
        Self {
            recipient: recipient.to_string(),
            event_type: event_type.to_string(),
            scope_key,
            idempotency_key: format!(
                "structured:{recipient}:{event_type}:{}",
                scope_key
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "none".to_string())
            ),
        }
    }

    fn freeform(recipient: &str) -> Self {
        Self {
            recipient: recipient.to_string(),
            event_type: "notify_parent_freeform".to_string(),
            scope_key: None,
            idempotency_key: uuid::Uuid::new_v4().to_string(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct InboxMessage {
    pub id: u64,
    pub target: String,
    pub project_dir: PathBuf,
    pub from: String,
    pub recipient: String,
    pub body: String,
    pub detail: String,
    pub injection_options: InjectionOptions,
    dedup_key: DedupKey,
}

impl InboxMessage {
    pub fn new(
        target: String,
        project_dir: PathBuf,
        from: String,
        recipient: String,
        body: String,
        detail: String,
    ) -> Self {
        let dedup_key = dedup_key_for_message(&from, &recipient, &body);
        Self {
            id: 0,
            target,
            project_dir,
            from,
            recipient,
            body,
            detail,
            injection_options: InjectionOptions::claude_default(),
            dedup_key,
        }
    }

    pub fn with_injection_options(mut self, injection_options: InjectionOptions) -> Self {
        self.injection_options = injection_options;
        self
    }

    pub fn with_idempotency_key(mut self, idempotency_key: impl Into<String>) -> Self {
        self.dedup_key.idempotency_key = idempotency_key.into();
        self
    }
}

/// Return the stable key for a structured message or a fresh UUID for a
/// free-form message. Callers persist this key with the durable batch.
pub fn idempotency_key_for_message(from: &str, recipient: &str, body: &str) -> String {
    dedup_key_for_message(from, recipient, body).idempotency_key
}

fn dedup_key_for_message(_from: &str, recipient: &str, body: &str) -> DedupKey {
    for (tag, event_type) in [
        ("[MERGE READY]", "MergeReady"),
        ("[PR READY]", "ReviewApproved"),
        ("[FIXES PUSHED]", "FixesPushed"),
        ("[COMMITS PUSHED]", "CommitsPushed"),
        ("[REVIEW TIMEOUT]", "ReviewTimeout"),
        ("[CI Status]", "CIStatus"),
    ] {
        if body.contains(tag) {
            return DedupKey::structured(recipient, event_type, parse_pr_number(body));
        }
    }

    if body.contains("## Review on PR #") || body.contains("[CHANGES REQUESTED] PR #") {
        return DedupKey::structured(recipient, "ReviewReceived", parse_pr_number(body));
    }

    if let Some(scope_key) = parse_stuck_scope(body) {
        return DedupKey::structured(recipient, "Stuck", Some(scope_key));
    }

    DedupKey::freeform(recipient)
}

fn append_inbox_event(
    project_dir: &std::path::Path,
    agent_id: &str,
    event_type: &str,
    data: serde_json::Value,
) {
    let Ok(log) = crate::services::EventLog::open(project_dir.join(".exo/logs")) else {
        return;
    };
    let _ = log.append(event_type, agent_id, &data);
}

fn parse_pr_number(body: &str) -> Option<u64> {
    let (_, after) = body.split_once("PR #")?;
    parse_leading_u64(after)
}

fn parse_stuck_scope(body: &str) -> Option<u64> {
    let (_, after) = body.split_once("[STUCK: ")?;
    let after = after.strip_prefix("PR #").unwrap_or(after);
    parse_leading_u64(after)
}

fn parse_leading_u64(input: &str) -> Option<u64> {
    let digits: String = input.chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        None
    } else {
        digits.parse().ok()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnqueueOutcome {
    pub depth: usize,
    pub warning_emitted: bool,
    pub should_start_consumer: bool,
    pub dropped_as_duplicate: bool,
}

#[derive(Debug, Default)]
struct AgentQueue {
    messages: VecDeque<InboxMessage>,
    consumer_active: bool,
    pending: HashSet<DedupKey>,
}

/// Process-local transport cache rebuilt from durable guidance rows.
///
/// SQLite's guidance queue owns lifecycle, ordering, retry, and terminal
/// state. This FIFO only serializes runtime injection attempts between cache
/// rebuilds.
#[derive(Debug)]
pub struct AgentInbox {
    queues: Mutex<HashMap<String, AgentQueue>>,
    next_id: AtomicU64,
    warning_threshold: usize,
    hard_cap: usize,
}

impl Default for AgentInbox {
    fn default() -> Self {
        Self::new(DEFAULT_WARNING_THRESHOLD, DEFAULT_HARD_CAP)
    }
}

impl AgentInbox {
    pub fn new(warning_threshold: usize, hard_cap: usize) -> Self {
        Self {
            queues: Mutex::new(HashMap::new()),
            next_id: AtomicU64::new(1),
            warning_threshold,
            hard_cap,
        }
    }

    pub async fn enqueue(&self, agent: &str, mut message: InboxMessage) -> Result<EnqueueOutcome> {
        let mut queues = self.queues.lock().await;
        let queue = queues.entry(agent.to_string()).or_default();

        if queue.pending.contains(&message.dedup_key) {
            append_inbox_event(
                &message.project_dir,
                &message.recipient,
                "agent_inbox.duplicates_dropped",
                serde_json::json!({
                    "recipient": message.recipient,
                    "event_type": message.dedup_key.event_type,
                    "scope_key": message.dedup_key.scope_key,
                    "idempotency_key": message.dedup_key.idempotency_key,
                    "outcome": "dropped"
                }),
            );
            tracing::debug!(
                recipient = %message.recipient,
                event_type = %message.dedup_key.event_type,
                scope_key = ?message.dedup_key.scope_key,
                idempotency_key = %message.dedup_key.idempotency_key,
                "dropping duplicate pending agent inbox message"
            );
            tracing::info!(
                otel.name = "agent_inbox.duplicates_dropped",
                recipient = %message.recipient,
                event_type = %message.dedup_key.event_type,
                scope_key = ?message.dedup_key.scope_key,
                idempotency_key = %message.dedup_key.idempotency_key,
                "[metric] agent_inbox.duplicates_dropped"
            );
            return Ok(EnqueueOutcome {
                depth: queue.messages.len(),
                warning_emitted: false,
                should_start_consumer: !queue.consumer_active && !queue.messages.is_empty(),
                dropped_as_duplicate: true,
            });
        }

        if queue.messages.len() >= self.hard_cap {
            return Err(anyhow!(
                "agent inbox for `{}` is full ({} queued, cap {})",
                agent,
                queue.messages.len(),
                self.hard_cap
            ));
        }

        message.id = self.next_id.fetch_add(1, Ordering::Relaxed);
        queue.pending.insert(message.dedup_key.clone());
        queue.messages.push_back(message);
        let depth = queue.messages.len();
        let should_start_consumer = !queue.consumer_active;
        Ok(EnqueueOutcome {
            depth,
            warning_emitted: depth >= self.warning_threshold,
            should_start_consumer,
            dropped_as_duplicate: false,
        })
    }

    /// Rebuild this transport cache from durable guidance after a process restart.
    ///
    /// SQLite remains authoritative: expired leases are recovered there first,
    /// and only available pending batches are copied into the FIFO. The durable
    /// batch ID is used as the cache idempotency key, so repeating reconstruction
    /// cannot create a second in-memory delivery.
    pub async fn rebuild_from_durable(
        &self,
        store: &InboxStore,
        agent: &str,
        target: &str,
        project_dir: PathBuf,
        injection_options: InjectionOptions,
    ) -> Result<usize> {
        self.rebuild_from_durable_at(
            store,
            agent,
            target,
            project_dir,
            injection_options,
            now_epoch_secs(),
        )
        .await
    }

    async fn rebuild_from_durable_at(
        &self,
        store: &InboxStore,
        agent: &str,
        target: &str,
        project_dir: PathBuf,
        injection_options: InjectionOptions,
        now: i64,
    ) -> Result<usize> {
        store.recover_expired_leases(now)?;
        let batches = store.pending_batches_for_agent(agent, now)?;
        let mut restored = 0;
        for batch in batches {
            let message = message_from_batch(&batch, target, &project_dir, injection_options)?;
            let outcome = self.enqueue(agent, message).await?;
            if !outcome.dropped_as_duplicate {
                restored += 1;
            }
        }
        Ok(restored)
    }

    pub async fn begin_delivery(&self, agent: &str) -> Option<InboxMessage> {
        let mut queues = self.queues.lock().await;
        let queue = queues.get_mut(agent)?;
        if queue.consumer_active {
            return None;
        }
        let message = queue.messages.front()?.clone();
        queue.consumer_active = true;
        Some(message)
    }

    pub async fn complete_delivery(&self, agent: &str, message_id: u64, success: bool) {
        let mut queues = self.queues.lock().await;
        let Some(queue) = queues.get_mut(agent) else {
            return;
        };

        if success
            && queue
                .messages
                .front()
                .is_some_and(|message| message.id == message_id)
        {
            if let Some(message) = queue.messages.pop_front() {
                queue.pending.remove(&message.dedup_key);
            }
        }

        queue.consumer_active = false;
    }

    /// Drop the head message after delivery attempts are exhausted. Clears the
    /// dedup `pending` entry without marking it recently-delivered, so an
    /// identical event can be re-enqueued immediately.
    pub async fn abandon_delivery(&self, agent: &str, message_id: u64) {
        let mut queues = self.queues.lock().await;
        let Some(queue) = queues.get_mut(agent) else {
            return;
        };

        if queue
            .messages
            .front()
            .is_some_and(|message| message.id == message_id)
        {
            if let Some(message) = queue.messages.pop_front() {
                queue.pending.remove(&message.dedup_key);
            }
        }

        queue.consumer_active = false;
    }

    pub async fn queue_depth(&self, agent: &str) -> usize {
        self.queues
            .lock()
            .await
            .get(agent)
            .map(|queue| queue.messages.len())
            .unwrap_or(0)
    }

    pub async fn is_consumer_active(&self, agent: &str) -> bool {
        self.queues
            .lock()
            .await
            .get(agent)
            .map(|queue| queue.consumer_active)
            .unwrap_or(false)
    }
}

fn message_from_batch(
    batch: &GuidanceBatch,
    target: &str,
    project_dir: &std::path::Path,
    injection_options: InjectionOptions,
) -> Result<InboxMessage> {
    let first_item = batch.items.first().context("guidance batch has no items")?;
    let body = batch
        .items
        .iter()
        .map(|item| item.content.as_str())
        .collect::<Vec<_>>()
        .join("\n\n");
    let summary = batch
        .items
        .iter()
        .filter_map(|item| item.summary.as_deref())
        .next()
        .unwrap_or("durable guidance")
        .to_string();
    let idempotency_key = batch
        .idempotency_key
        .clone()
        .unwrap_or_else(|| batch.batch_id.clone());
    Ok(InboxMessage::new(
        target.to_string(),
        project_dir.to_path_buf(),
        first_item.from_agent.clone(),
        batch.agent_id.clone(),
        body,
        format!("durable guidance batch {}: {summary}", batch.batch_id),
    )
    .with_injection_options(injection_options)
    .with_idempotency_key(idempotency_key))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::guidance_adapters::BoundaryEvidence;
    use crate::services::guidance_queue::{
        GuidanceBatchRequest, GuidanceConsumer, GuidanceIdentity, GuidanceItemInput, QueueClass,
    };
    use crate::services::InboxStore;
    use serde_json::json;
    use tempfile::TempDir;

    fn message(body: &str) -> InboxMessage {
        InboxMessage::new(
            "%1".to_string(),
            PathBuf::from("."),
            "sender".to_string(),
            "agent".to_string(),
            body.to_string(),
            "%1".to_string(),
        )
    }

    fn durable_request(agent: &str, key: &str, body: &str) -> GuidanceBatchRequest {
        GuidanceBatchRequest {
            agent_id: agent.to_string(),
            queue_class: QueueClass::Steering,
            items: vec![GuidanceItemInput {
                from_agent: "root".to_string(),
                content: body.to_string(),
                summary: Some(format!("summary-{key}")),
                injection_options: json!({"submit": true}),
            }],
            identity: GuidanceIdentity::default(),
            idempotency_key: Some(key.to_string()),
            source_message_id: None,
        }
    }

    #[test]
    fn message_defaults_to_claude_file_indirect_submit() {
        let message = message("body");
        assert_eq!(
            message.injection_options,
            InjectionOptions::claude_default()
        );
    }

    #[test]
    fn message_can_disable_file_indirect_for_non_claude_tmux_delivery() {
        let message = message("body").with_injection_options(InjectionOptions::inline_submit());
        assert_eq!(message.injection_options, InjectionOptions::inline_submit());
    }

    #[tokio::test]
    async fn enqueue_dequeue_fifo_order() {
        let inbox = AgentInbox::new(8, 32);
        inbox.enqueue("agent", message("first")).await.unwrap();
        inbox.enqueue("agent", message("second")).await.unwrap();

        let first = inbox.begin_delivery("agent").await.unwrap();
        assert_eq!(first.body, "first");
        inbox.complete_delivery("agent", first.id, true).await;

        let second = inbox.begin_delivery("agent").await.unwrap();
        assert_eq!(second.body, "second");
    }

    #[tokio::test]
    async fn rebuild_from_durable_restores_pending_batches_in_order_once() -> Result<()> {
        let directory = TempDir::new()?;
        let store = InboxStore::open(directory.path())?;
        store.enqueue_batch(durable_request("agent", "first", "first body"))?;
        store.enqueue_batch(durable_request("agent", "second", "second body"))?;
        let inbox = AgentInbox::new(8, 32);

        let restored = inbox
            .rebuild_from_durable(
                &store,
                "agent",
                "%42",
                directory.path().to_path_buf(),
                InjectionOptions::inline_submit(),
            )
            .await?;
        assert_eq!(restored, 2);
        assert_eq!(inbox.queue_depth("agent").await, 2);
        let restored_again = inbox
            .rebuild_from_durable(
                &store,
                "agent",
                "%42",
                directory.path().to_path_buf(),
                InjectionOptions::inline_submit(),
            )
            .await?;
        assert_eq!(restored_again, 0);
        assert_eq!(inbox.queue_depth("agent").await, 2);
        let first = inbox.begin_delivery("agent").await.context("first batch")?;
        assert_eq!(first.body, "first body");
        assert_eq!(first.target, "%42");
        inbox.complete_delivery("agent", first.id, true).await;
        let second = inbox
            .begin_delivery("agent")
            .await
            .context("second batch")?;
        assert_eq!(second.body, "second body");

        assert_eq!(inbox.queue_depth("agent").await, 1);
        Ok(())
    }

    #[tokio::test]
    async fn rebuild_recovers_expired_lease_before_backoff_window() -> Result<()> {
        let directory = TempDir::new()?;
        let store = InboxStore::open(directory.path())?;
        let batch = store
            .enqueue_batch(durable_request("agent", "expired", "expired body"))?
            .batch;
        store
            .claim_next(
                &BoundaryEvidence::turn_finished("agent"),
                &GuidanceConsumer {
                    consumer_id: "consumer".to_string(),
                    invocation_id: None,
                    generation: None,
                },
                1,
            )?
            .context("lease")?;
        let inbox = AgentInbox::new(8, 32);
        let recovery_now = now_epoch_secs() + 301;
        let restored = inbox
            .rebuild_from_durable_at(
                &store,
                "agent",
                "%42",
                directory.path().to_path_buf(),
                InjectionOptions::inline_submit(),
                recovery_now,
            )
            .await?;
        assert_eq!(restored, 0, "recovery applies bounded retry backoff");
        assert_eq!(
            store
                .inspect_batch(&batch.batch_id)?
                .context("batch")?
                .state,
            crate::services::GuidanceState::Pending
        );
        store.connection()?.execute(
            "UPDATE guidance_batches SET available_at = ?1 WHERE batch_id = ?2",
            rusqlite::params![recovery_now, batch.batch_id],
        )?;
        let restored = inbox
            .rebuild_from_durable_at(
                &store,
                "agent",
                "%42",
                directory.path().to_path_buf(),
                InjectionOptions::inline_submit(),
                recovery_now,
            )
            .await?;
        assert_eq!(restored, 1);
        assert_eq!(inbox.queue_depth("agent").await, 1);
        Ok(())
    }

    #[tokio::test]
    async fn single_consumer_per_agent_even_with_concurrent_enqueues() {
        let inbox = AgentInbox::new(8, 32);
        let first = inbox.enqueue("agent", message("first")).await.unwrap();
        let second = inbox.enqueue("agent", message("second")).await.unwrap();
        assert!(first.should_start_consumer);
        assert!(second.should_start_consumer);

        assert!(inbox.begin_delivery("agent").await.is_some());
        assert!(inbox.begin_delivery("agent").await.is_none());
        assert!(inbox.is_consumer_active("agent").await);
    }

    #[tokio::test]
    async fn different_agents_have_independent_consumers() {
        let inbox = AgentInbox::new(8, 32);
        inbox.enqueue("agent-a", message("a")).await.unwrap();
        inbox.enqueue("agent-b", message("b")).await.unwrap();

        assert!(inbox.begin_delivery("agent-a").await.is_some());
        assert!(inbox.begin_delivery("agent-b").await.is_some());
        assert!(inbox.is_consumer_active("agent-a").await);
        assert!(inbox.is_consumer_active("agent-b").await);
    }

    #[tokio::test]
    async fn queue_depth_warning_emitted_at_threshold() {
        let inbox = AgentInbox::new(2, 32);
        assert!(
            !inbox
                .enqueue("agent", message("one"))
                .await
                .unwrap()
                .warning_emitted
        );
        assert!(
            inbox
                .enqueue("agent", message("two"))
                .await
                .unwrap()
                .warning_emitted
        );
        assert_eq!(inbox.queue_depth("agent").await, 2);
    }

    #[tokio::test]
    async fn enqueue_rejects_when_hard_cap_reached() {
        let inbox = AgentInbox::new(8, 1);
        inbox.enqueue("agent", message("one")).await.unwrap();
        assert!(inbox.enqueue("agent", message("two")).await.is_err());
    }

    #[tokio::test]
    async fn failed_delivery_keeps_message_at_head_for_retry() {
        let inbox = AgentInbox::new(8, 32);
        inbox.enqueue("agent", message("first")).await.unwrap();
        let first = inbox.begin_delivery("agent").await.unwrap();
        inbox.complete_delivery("agent", first.id, false).await;

        let retry = inbox.begin_delivery("agent").await.unwrap();
        assert_eq!(retry.id, first.id);
        assert_eq!(retry.body, "first");
    }

    #[tokio::test]
    async fn duplicate_after_failed_delivery_still_requests_a_consumer() {
        let inbox = AgentInbox::new(8, 32);
        let body = "[MERGE READY] PR #42 approved";

        let first = inbox.enqueue("agent", message(body)).await.unwrap();
        assert!(first.should_start_consumer);

        // Consumer runs, delivery fails, consumer task exits (delivery.rs:814).
        let queued = inbox.begin_delivery("agent").await.unwrap();
        inbox.complete_delivery("agent", queued.id, false).await;
        assert_eq!(inbox.queue_depth("agent").await, 1);
        assert!(!inbox.is_consumer_active("agent").await);

        // A repeat of the same event is the only thing that could restart the
        // consumer; it must not leave the queue stranded.
        let second = inbox.enqueue("agent", message(body)).await.unwrap();
        assert!(second.dropped_as_duplicate);
        assert!(
            second.should_start_consumer,
            "stranded queue (depth {}) with no active consumer must request a restart",
            second.depth
        );
    }

    #[tokio::test]
    async fn successful_delivery_pops_head_before_next_message() {
        let inbox = AgentInbox::new(8, 32);
        inbox.enqueue("agent", message("first")).await.unwrap();
        inbox.enqueue("agent", message("second")).await.unwrap();
        let first = inbox.begin_delivery("agent").await.unwrap();
        inbox.complete_delivery("agent", first.id, true).await;

        let second = inbox.begin_delivery("agent").await.unwrap();
        assert_eq!(second.body, "second");
    }

    #[tokio::test]
    async fn abandoned_delivery_drains_queue_and_allows_reenqueue() {
        let inbox = AgentInbox::new(8, 32);
        let body = "[MERGE READY] PR #42 approved";
        inbox.enqueue("agent", message(body)).await.unwrap();

        let queued = inbox.begin_delivery("agent").await.unwrap();
        inbox.abandon_delivery("agent", queued.id).await;

        assert_eq!(inbox.queue_depth("agent").await, 0);
        assert!(!inbox.is_consumer_active("agent").await);

        // Abandoned messages are not marked recently-delivered, so an identical
        // event is accepted rather than suppressed by the dedup window.
        let retry = inbox.enqueue("agent", message(body)).await.unwrap();
        assert!(!retry.dropped_as_duplicate);
        assert!(retry.should_start_consumer);
        assert_eq!(inbox.queue_depth("agent").await, 1);
    }

    #[tokio::test]
    async fn abandon_delivery_ignores_stale_message_id() {
        let inbox = AgentInbox::new(8, 32);
        inbox.enqueue("agent", message("first")).await.unwrap();
        let queued = inbox.begin_delivery("agent").await.unwrap();

        inbox.abandon_delivery("agent", queued.id + 999).await;

        assert_eq!(inbox.queue_depth("agent").await, 1);
    }

    #[tokio::test]
    async fn consumer_task_exits_when_queue_empty_and_restarts_on_new_message() {
        let inbox = AgentInbox::new(8, 32);
        inbox.enqueue("agent", message("first")).await.unwrap();
        let first = inbox.begin_delivery("agent").await.unwrap();
        inbox.complete_delivery("agent", first.id, true).await;
        assert!(!inbox.is_consumer_active("agent").await);

        let outcome = inbox.enqueue("agent", message("second")).await.unwrap();
        assert!(outcome.should_start_consumer);
    }

    #[tokio::test]
    async fn duplicate_structured_identity_is_dropped_while_pending() {
        let inbox = AgentInbox::new(8, 32);
        let body =
            "[MERGE READY] PR #42 on branch main.a has CI status success and reviewer approval.";

        let first = inbox.enqueue("agent", message(body)).await.unwrap();
        let second = inbox.enqueue("agent", message(body)).await.unwrap();

        assert!(!first.dropped_as_duplicate);
        assert!(second.dropped_as_duplicate);
        assert_eq!(second.depth, 1);
        assert_eq!(inbox.queue_depth("agent").await, 1);
    }

    #[tokio::test]
    async fn duplicate_structured_identity_is_allowed_after_delivery() {
        let inbox = AgentInbox::new(8, 32);
        let body =
            "[MERGE READY] PR #42 on branch main.a has CI status success and reviewer approval.";
        inbox.enqueue("agent", message(body)).await.unwrap();
        let first = inbox.begin_delivery("agent").await.unwrap();
        inbox.complete_delivery("agent", first.id, true).await;

        let outcome = inbox.enqueue("agent", message(body)).await.unwrap();

        assert!(!outcome.dropped_as_duplicate);
        assert_eq!(inbox.queue_depth("agent").await, 1);
    }

    #[tokio::test]
    async fn identical_freeform_bodies_receive_distinct_identities() {
        let inbox = AgentInbox::new(8, 32);
        let first = inbox.enqueue("agent", message("same body")).await.unwrap();
        let second = inbox.enqueue("agent", message("same body")).await.unwrap();

        assert!(!first.dropped_as_duplicate);
        assert!(!second.dropped_as_duplicate);
        assert_eq!(inbox.queue_depth("agent").await, 2);
    }

    #[tokio::test]
    async fn different_structural_keys_are_not_deduped() {
        let inbox = AgentInbox::new(8, 32);
        inbox
            .enqueue("agent", message("[MERGE READY] PR #42 on branch main.a"))
            .await
            .unwrap();
        let outcome = inbox
            .enqueue("agent", message("[MERGE READY] PR #43 on branch main.b"))
            .await
            .unwrap();

        assert!(!outcome.dropped_as_duplicate);
        assert_eq!(inbox.queue_depth("agent").await, 2);
    }

    #[tokio::test]
    async fn failed_delivery_keeps_identity_pending_for_retry() {
        let inbox = AgentInbox::new(8, 32);
        let body = "[MERGE READY] PR #42 on branch main.a";
        inbox.enqueue("agent", message(body)).await.unwrap();
        let first = inbox.begin_delivery("agent").await.unwrap();
        inbox.complete_delivery("agent", first.id, false).await;
        let retry = inbox.begin_delivery("agent").await.unwrap();

        assert_eq!(retry.id, first.id);
        assert_eq!(retry.body, body);
    }
}
