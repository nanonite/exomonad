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
    /// A durable batch identity used only to avoid copying one queued row
    /// into this process-local cache more than once at a time.
    cache_key: Option<String>,
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
        Self {
            id: 0,
            target,
            project_dir,
            from,
            recipient,
            body,
            detail,
            injection_options: InjectionOptions::claude_default(),
            cache_key: None,
        }
    }

    pub fn with_injection_options(mut self, injection_options: InjectionOptions) -> Self {
        self.injection_options = injection_options;
        self
    }

    pub fn with_cache_key(mut self, cache_key: impl Into<String>) -> Self {
        self.cache_key = Some(cache_key.into());
        self
    }
}

/// Return the durable producer identity for a structured message or a fresh
/// UUID for a free-form message. This key is persisted with the durable batch;
/// it is deliberately not used by the process-local transport cache.
pub fn idempotency_key_for_message(from: &str, recipient: &str, body: &str) -> String {
    for (tag, event_type) in [
        ("[MERGE READY]", "MergeReady"),
        ("[PR READY]", "ReviewApproved"),
        ("[FIXES PUSHED]", "FixesPushed"),
        ("[COMMITS PUSHED]", "CommitsPushed"),
        ("[REVIEW TIMEOUT]", "ReviewTimeout"),
        ("[CI Status]", "CIStatus"),
    ] {
        if body.contains(tag) {
            return format!(
                "structured:{recipient}:{event_type}:{}",
                parse_pr_number(body)
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "none".to_string())
            );
        }
    }

    if body.contains("## Review on PR #") || body.contains("[CHANGES REQUESTED] PR #") {
        return format!(
            "structured:{recipient}:ReviewReceived:{}",
            parse_pr_number(body)
                .map(|value| value.to_string())
                .unwrap_or_else(|| "none".to_string())
        );
    }

    if let Some(scope_key) = parse_stuck_scope(body) {
        return format!("structured:{recipient}:Stuck:{scope_key}");
    }

    let _ = from;
    uuid::Uuid::new_v4().to_string()
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
    /// Cache-local identities for durable batches currently represented in the
    /// FIFO. SQLite remains the authority for idempotency and lifecycle.
    queued_cache_keys: HashSet<String>,
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

        if let Some(cache_key) = message.cache_key.as_deref() {
            if queue.queued_cache_keys.contains(cache_key) {
                append_inbox_event(
                    &message.project_dir,
                    &message.recipient,
                    "agent_inbox.duplicates_dropped",
                    serde_json::json!({
                        "recipient": message.recipient,
                        "cache_key": cache_key,
                        "outcome": "dropped",
                        "authority": "transport_cache"
                    }),
                );
                tracing::debug!(
                    recipient = %message.recipient,
                    cache_key,
                    "dropping duplicate durable batch already present in transport cache"
                );
                tracing::info!(
                    otel.name = "agent_inbox.duplicates_dropped",
                    recipient = %message.recipient,
                    cache_key,
                    authority = "transport_cache",
                    "[metric] agent_inbox.duplicates_dropped"
                );
                return Ok(EnqueueOutcome {
                    depth: queue.messages.len(),
                    warning_emitted: false,
                    should_start_consumer: !queue.consumer_active && !queue.messages.is_empty(),
                    dropped_as_duplicate: true,
                });
            }
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
        if let Some(cache_key) = message.cache_key.as_ref() {
            queue.queued_cache_keys.insert(cache_key.clone());
        }
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
    /// The durable batch ID is used only as a cache key, so repeating
    /// reconstruction cannot create a second in-memory delivery while the
    /// first copy remains queued.
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
                if let Some(cache_key) = message.cache_key {
                    queue.queued_cache_keys.remove(&cache_key);
                }
            }
        }

        queue.consumer_active = false;
    }

    /// Drop the head message after delivery attempts are exhausted. Clears its
    /// cache-local key so a later durable rebuild can retry the batch.
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
                if let Some(cache_key) = message.cache_key {
                    queue.queued_cache_keys.remove(&cache_key);
                }
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
    Ok(InboxMessage::new(
        target.to_string(),
        project_dir.to_path_buf(),
        first_item.from_agent.clone(),
        batch.agent_id.clone(),
        body,
        format!("durable guidance batch {}: {summary}", batch.batch_id),
    )
    .with_injection_options(injection_options)
    .with_cache_key(batch.batch_id.clone()))
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

    fn cached_message(body: &str, cache_key: &str) -> InboxMessage {
        message(body).with_cache_key(cache_key)
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
    async fn failed_delivery_preserves_message_identity_for_retry() {
        let inbox = AgentInbox::new(8, 32);
        inbox.enqueue("agent", message("first")).await.unwrap();
        let first = inbox.begin_delivery("agent").await.unwrap();
        inbox.complete_delivery("agent", first.id, false).await;

        let retry = inbox.begin_delivery("agent").await.unwrap();
        assert_eq!(retry.id, first.id);
        assert_eq!(retry.body, "first");
    }

    #[tokio::test]
    async fn repeated_body_after_failed_delivery_is_queued_again() {
        let inbox = AgentInbox::new(8, 32);
        let body = "[MERGE READY] PR #42 approved";

        let first = inbox.enqueue("agent", message(body)).await.unwrap();
        assert!(first.should_start_consumer);

        // Consumer runs, delivery fails, consumer task exits (delivery.rs:814).
        let queued = inbox.begin_delivery("agent").await.unwrap();
        inbox.complete_delivery("agent", queued.id, false).await;
        assert_eq!(inbox.queue_depth("agent").await, 1);
        assert!(!inbox.is_consumer_active("agent").await);

        // The cache no longer infers identity from the body. Durable SQLite
        // decides whether this is a duplicate before transport enqueue.
        let second = inbox.enqueue("agent", message(body)).await.unwrap();
        assert!(!second.dropped_as_duplicate);
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

        // An abandoned durable batch may be rebuilt for retry.
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
    async fn structured_message_bodies_do_not_control_cache_identity() {
        let inbox = AgentInbox::new(8, 32);
        let body =
            "[MERGE READY] PR #42 on branch main.a has CI status success and reviewer approval.";

        let first = inbox.enqueue("agent", message(body)).await.unwrap();
        let second = inbox.enqueue("agent", message(body)).await.unwrap();

        assert!(!first.dropped_as_duplicate);
        assert!(!second.dropped_as_duplicate);
        assert_eq!(second.depth, 2);
        assert_eq!(inbox.queue_depth("agent").await, 2);
    }

    #[tokio::test]
    async fn durable_batch_cache_key_only_suppresses_same_queued_batch() {
        let inbox = AgentInbox::new(8, 32);
        let first = inbox
            .enqueue("agent", cached_message("first durable body", "batch-42"))
            .await
            .unwrap();
        let duplicate = inbox
            .enqueue(
                "agent",
                cached_message("same batch, different body", "batch-42"),
            )
            .await
            .unwrap();

        assert!(!first.dropped_as_duplicate);
        assert!(duplicate.dropped_as_duplicate);
        assert_eq!(duplicate.depth, 1);

        let first = inbox.begin_delivery("agent").await.unwrap();
        inbox.complete_delivery("agent", first.id, true).await;

        let outcome = inbox
            .enqueue(
                "agent",
                cached_message("retry after cache eviction", "batch-42"),
            )
            .await
            .unwrap();

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
    async fn failed_delivery_keeps_message_at_head_for_retry() {
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
