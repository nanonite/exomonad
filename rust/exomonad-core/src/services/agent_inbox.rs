use anyhow::{anyhow, Result};
use std::collections::hash_map::DefaultHasher;
use std::collections::{HashMap, HashSet, VecDeque};
use std::hash::{Hash, Hasher};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::LazyLock;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

const DEFAULT_WARNING_THRESHOLD: usize = 8;
const DEFAULT_HARD_CAP: usize = 32;
const DEFAULT_DEDUP_WINDOW: Duration = Duration::from_secs(30);

pub static GLOBAL_AGENT_INBOX: LazyLock<AgentInbox> = LazyLock::new(AgentInbox::default);

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct DedupKey {
    recipient: String,
    event_type: String,
    scope_key: Option<u64>,
    payload_hash: Option<u64>,
}

impl DedupKey {
    fn structured(recipient: &str, event_type: &str, scope_key: Option<u64>) -> Self {
        Self {
            recipient: recipient.to_string(),
            event_type: event_type.to_string(),
            scope_key,
            payload_hash: None,
        }
    }

    fn freeform(from: &str, recipient: &str, body: &str) -> Self {
        let mut hasher = DefaultHasher::new();
        from.hash(&mut hasher);
        recipient.hash(&mut hasher);
        body.hash(&mut hasher);
        Self {
            recipient: recipient.to_string(),
            event_type: "notify_parent_freeform".to_string(),
            scope_key: None,
            payload_hash: Some(hasher.finish()),
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
            dedup_key,
        }
    }
}

fn dedup_key_for_message(from: &str, recipient: &str, body: &str) -> DedupKey {
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

    DedupKey::freeform(from, recipient, body)
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
    recent: HashMap<DedupKey, Instant>,
}

impl AgentQueue {
    fn prune_recent(&mut self, now: Instant, dedup_window: Duration) {
        self.recent
            .retain(|_, delivered_at| now.duration_since(*delivered_at) < dedup_window);
    }
}

#[derive(Debug)]
pub struct AgentInbox {
    queues: Mutex<HashMap<String, AgentQueue>>,
    next_id: AtomicU64,
    warning_threshold: usize,
    hard_cap: usize,
    dedup_window: Duration,
}

impl Default for AgentInbox {
    fn default() -> Self {
        Self::new(DEFAULT_WARNING_THRESHOLD, DEFAULT_HARD_CAP)
    }
}

impl AgentInbox {
    pub fn new(warning_threshold: usize, hard_cap: usize) -> Self {
        Self::new_with_dedup_window(warning_threshold, hard_cap, DEFAULT_DEDUP_WINDOW)
    }

    pub fn new_with_dedup_window(
        warning_threshold: usize,
        hard_cap: usize,
        dedup_window: Duration,
    ) -> Self {
        Self {
            queues: Mutex::new(HashMap::new()),
            next_id: AtomicU64::new(1),
            warning_threshold,
            hard_cap,
            dedup_window,
        }
    }

    pub async fn enqueue(&self, agent: &str, mut message: InboxMessage) -> Result<EnqueueOutcome> {
        let mut queues = self.queues.lock().await;
        let queue = queues.entry(agent.to_string()).or_default();
        let now = Instant::now();
        queue.prune_recent(now, self.dedup_window);

        if queue.pending.contains(&message.dedup_key)
            || queue.recent.contains_key(&message.dedup_key)
        {
            tracing::debug!(
                recipient = %message.recipient,
                event_type = %message.dedup_key.event_type,
                scope_key = ?message.dedup_key.scope_key,
                "dropping duplicate agent inbox message within dedup window"
            );
            tracing::info!(
                otel.name = "agent_inbox.duplicates_dropped",
                recipient = %message.recipient,
                event_type = %message.dedup_key.event_type,
                scope_key = ?message.dedup_key.scope_key,
                "[metric] agent_inbox.duplicates_dropped"
            );
            return Ok(EnqueueOutcome {
                depth: queue.messages.len(),
                warning_emitted: false,
                should_start_consumer: false,
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

        let mut delivered_key = None;
        if success
            && queue
                .messages
                .front()
                .is_some_and(|message| message.id == message_id)
        {
            if let Some(message) = queue.messages.pop_front() {
                queue.pending.remove(&message.dedup_key);
                delivered_key = Some(message.dedup_key);
            }
        }

        if let Some(key) = delivered_key {
            queue.recent.insert(key, Instant::now());
        }

        queue.consumer_active = false;
        queue.prune_recent(Instant::now(), self.dedup_window);
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

#[cfg(test)]
mod tests {
    use super::*;

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
    async fn duplicate_structural_message_is_dropped_within_window() {
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
    async fn duplicate_structural_message_is_allowed_outside_window() {
        let inbox = AgentInbox::new_with_dedup_window(8, 32, Duration::from_millis(1));
        let body =
            "[MERGE READY] PR #42 on branch main.a has CI status success and reviewer approval.";
        inbox.enqueue("agent", message(body)).await.unwrap();
        let first = inbox.begin_delivery("agent").await.unwrap();
        inbox.complete_delivery("agent", first.id, true).await;
        tokio::time::sleep(Duration::from_millis(2)).await;

        let outcome = inbox.enqueue("agent", message(body)).await.unwrap();

        assert!(!outcome.dropped_as_duplicate);
        assert_eq!(inbox.queue_depth("agent").await, 1);
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
    async fn failed_delivery_does_not_mark_recent_dedup_window() {
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
