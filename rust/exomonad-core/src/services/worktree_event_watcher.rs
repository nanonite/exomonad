use crate::domain::{AgentName, BranchName, CIStatus, PRNumber};
use crate::plugin_manager::PluginManager;
use crate::services::agent_control::AgentType;
use crate::services::file_pr_local::{read_pr_registry, LocalReviewState, PrState};
use crate::services::{
    HasAcpRegistry, HasAgentResolver, HasEventLog, HasEventQueue, HasOpencodeAcpRegistry,
    HasProjectDir, HasTeamRegistry,
};
use anyhow::Result;
use exomonad_proto::effects::events::{event::EventType, AgentMessage, Event};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::process::Command;
use tokio::sync::{Mutex, RwLock};
use tracing::{debug, info, instrument, warn};

type PluginMap = Arc<RwLock<HashMap<AgentName, Arc<PluginManager>>>>;

/// Review state derived from local review files.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum ReviewState {
    None,
    ChangesRequested,
    Approved,
}

/// A review comment from a local review file.
#[derive(Debug, Clone, Serialize)]
struct LocalReviewComment {
    body: String,
    path: Option<String>,
    diff_hunk: Option<String>,
}

/// A local review with typed state.
#[derive(Debug, Clone, Serialize)]
struct LocalReview {
    body: String,
    state: ReviewState,
}

#[derive(Debug, Clone, Serialize)]
enum PendingAction {
    WasmEvent {
        event_type: &'static str,
        payload: serde_json::Value,
    },
    EmitEvent {
        status: String,
        message: String,
        comments: Option<Vec<LocalReviewComment>>,
        reviews: Option<Vec<LocalReview>>,
    },
}

/// Per-PR state tracked across poll cycles.
#[derive(Debug, Clone)]
struct WatchState {
    last_comment_count: usize,
    last_ci_status: CIStatus,
    branch_name: BranchName,
    agent_type: AgentType,
    first_seen: Instant,
    notified_parent_timeout: bool,
    last_review_state: ReviewState,
    last_sha: String,
    notified_parent_approved: bool,
    addressed_changes: bool,
}

impl WatchState {
    fn new(branch: &BranchName, agent_type: AgentType, sha: &str, ci_status: CIStatus, comment_count: usize) -> Self {
        Self {
            last_comment_count: comment_count,
            last_ci_status: ci_status,
            branch_name: branch.clone(),
            agent_type,
            first_seen: Instant::now(),
            notified_parent_timeout: false,
            last_review_state: ReviewState::None,
            last_sha: sha.to_string(),
            notified_parent_approved: false,
            addressed_changes: false,
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(tag = "action")]
enum EventActionResponse {
    #[serde(rename = "inject_message")]
    InjectMessage { message: String },
    #[serde(rename = "notify_parent")]
    NotifyParent { message: String, pr_number: i64 },
    #[serde(rename = "no_action")]
    NoAction,
}

/// On-disk format for `.exo/reviews/pr_{N}.json`.
#[derive(Debug, Clone, Deserialize, Default)]
struct ReviewFile {
    #[serde(default)]
    state: String,
    #[serde(default)]
    comments: Vec<ReviewCommentEntry>,
}

#[derive(Debug, Clone, Deserialize)]
struct ReviewCommentEntry {
    #[serde(default)]
    body: String,
    #[serde(default)]
    path: Option<String>,
    #[serde(default)]
    line: Option<u64>,
    #[serde(default)]
    diff_hunk: Option<String>,
}

/// Observation collected from local sources for one open PR.
struct Observation {
    head_sha: String,
    review_state: LocalReviewState,
    comments: Vec<LocalReviewComment>,
    ci_status: CIStatus,
}

/// Replaces `github_poller.rs` and `copilot_review.rs` by observing the local
/// `.exo/prs.json` registry, `.exo/reviews/` files, and git worktree state.
pub struct WorktreeEventWatcher<C> {
    ctx: Arc<C>,
    poll_interval: Duration,
    state: Arc<Mutex<HashMap<u64, WatchState>>>,
    prs_path: std::path::PathBuf,
    plugins: Option<PluginMap>,
}

impl<C> WorktreeEventWatcher<C>
where
    C: HasTeamRegistry
        + HasAcpRegistry
        + HasOpencodeAcpRegistry
        + HasAgentResolver
        + HasEventLog
        + HasEventQueue
        + HasProjectDir
        + 'static,
{
    pub fn new(ctx: Arc<C>) -> Self {
        let prs_path = ctx.project_dir().join(".exo/prs.json");
        Self {
            ctx,
            poll_interval: Duration::from_secs(60),
            state: Arc::new(Mutex::new(HashMap::new())),
            prs_path,
            plugins: None,
        }
    }

    pub fn with_poll_interval(mut self, interval: Duration) -> Self {
        self.poll_interval = interval;
        self
    }

    pub fn with_plugins(mut self, plugins: PluginMap) -> Self {
        self.plugins = Some(plugins);
        self
    }

    pub async fn run(&self) {
        tracing::info!(
            poll_interval_secs = self.poll_interval.as_secs(),
            "Local worktree event watcher started"
        );

        let base_interval = self.poll_interval;
        let max_backoff = Duration::from_secs(600);
        let mut consecutive_failures: u32 = 0;

        loop {
            let sleep_duration = if consecutive_failures == 0 {
                base_interval
            } else {
                let backoff = base_interval * 2u32.saturating_pow(consecutive_failures.min(6));
                backoff.min(max_backoff)
            };

            tokio::time::sleep(sleep_duration).await;

            match self.poll_cycle().await {
                Ok(()) => {
                    if consecutive_failures > 0 {
                        info!(previous_failures = consecutive_failures, "Watcher recovered");
                    }
                    consecutive_failures = 0;
                }
                Err(e) => {
                    consecutive_failures += 1;
                    let next_retry_secs = {
                        let backoff = base_interval * 2u32.saturating_pow(consecutive_failures.min(6));
                        backoff.min(max_backoff).as_secs()
                    };
                    if consecutive_failures <= 3 {
                        warn!(consecutive_failures, next_retry_secs, "Watcher cycle failed: {}", e);
                    } else {
                        debug!(consecutive_failures, next_retry_secs, "Watcher cycle failed: {}", e);
                    }
                }
            }
        }
    }

    #[instrument(skip_all, name = "worktree_event_watcher.poll_cycle")]
    async fn poll_cycle(&self) -> Result<()> {
        let registry = match read_pr_registry(&self.prs_path).await {
            Ok(r) => r,
            Err(_) => return Ok(()),
        };

        if registry.prs.is_empty() {
            return Ok(());
        }

        let observations = self.collect_observations(&registry).await?;
        let removed = self.process_observations(&registry, &observations).await?;
        self.detect_merged(&registry, &removed).await?;

        Ok(())
    }

    async fn collect_observations(&self, registry: &crate::services::file_pr_local::PrRegistry) -> Result<HashMap<u64, Observation>> {
        let mut observations = HashMap::new();
        let project_dir = self.ctx.project_dir().to_path_buf();

        for (number, pr) in &registry.prs {
            if pr.state != PrState::Open {
                continue;
            }

            let worktree_path = project_dir
                .join(".exo/worktrees")
                .join(&pr.author_agent);

            let head_sha = git_head_sha(&worktree_path).await.unwrap_or_default();

            let review_file = Self::read_review_file(&project_dir, *number).await;
            let (review_state, comments) = match review_file {
                Some(rf) => {
                    let state = match rf.state.as_str() {
                        "approved" => LocalReviewState::Approved,
                        "changes_requested" => LocalReviewState::ChangesRequested,
                        _ => LocalReviewState::PendingReview,
                    };
                    let lrc: Vec<LocalReviewComment> = rf.comments.into_iter().map(|c| {
                        LocalReviewComment {
                            body: c.body,
                            path: c.path,
                            diff_hunk: c.diff_hunk,
                        }
                    }).collect();
                    (state, lrc)
                }
                None => {
                    let state = pr.review_state.clone();
                    (state, vec![])
                }
            };

            let ci_status = CIStatus::Unknown;

            observations.insert(*number, Observation {
                head_sha,
                review_state,
                comments,
                ci_status,
            });
        }

        Ok(observations)
    }

    async fn process_observations(
        &self,
        registry: &crate::services::file_pr_local::PrRegistry,
        observations: &HashMap<u64, Observation>,
    ) -> Result<Vec<u64>> {
        let mut removed_prs = Vec::new();
        let mut pending_actions: Vec<(u64, Vec<PendingAction>, BranchName, AgentType)> = Vec::new();

        {
            let mut state_guard = self.state.lock().await;

            for (pr_number, obs) in observations {
                let pr = match registry.prs.get(pr_number) {
                    Some(p) => p,
                    None => continue,
                };

                let agent_name = &pr.author_agent;
                let agent_type = AgentType::from_dir_name(agent_name);
                let branch = BranchName::from(pr.head_branch.as_str());
                let comment_count = obs.comments.len();
                let (local_reviews, _local_review_state) = obs_to_review_parts(obs);

                let actions = if let Some(old_state) = state_guard.get_mut(pr_number) {
                    compute_pr_actions(
                        old_state,
                        PRNumber::new(*pr_number),
                        &obs.head_sha,
                        &obs.comments,
                        &local_reviews,
                        obs.ci_status,
                        branch.as_str(),
                        &|c, r| format_review_message(c, r),
                    )
                } else {
                    state_guard.insert(
                        *pr_number,
                        WatchState::new(
                            &branch,
                            agent_type,
                            &obs.head_sha,
                            obs.ci_status,
                            comment_count,
                        ),
                    );
                    vec![]
                };

                if !actions.is_empty() {
                    pending_actions.push((*pr_number, actions, branch, agent_type));
                }
            }

            for pr_number in observations.keys() {
                if !registry.prs.contains_key(pr_number) {
                    removed_prs.push(*pr_number);
                }
            }
            for num in &removed_prs {
                state_guard.remove(num);
            }
        }

        for (_pr_number, actions, branch, agent_type) in pending_actions {
            for action in actions {
                match action {
                    PendingAction::WasmEvent { event_type, payload } => {
                        if let Ok(Some(response)) = self
                            .call_handle_event(branch.as_str(), agent_type, event_type, payload)
                            .await
                        {
                            self.handle_event_action(response, branch.as_str(), agent_type).await;
                        }
                    }
                    PendingAction::EmitEvent { status, message, comments, reviews } => {
                        self.emit_event(
                            branch.as_str(),
                            &status,
                            &message,
                            agent_type,
                            comments,
                            reviews,
                        )
                        .await;
                    }
                }
            }
        }

        Ok(removed_prs)
    }

    async fn detect_merged(
        &self,
        registry: &crate::services::file_pr_local::PrRegistry,
        removed: &[u64],
    ) -> Result<()> {
        if removed.is_empty() {
            return Ok(());
        }

        let state_guard = self.state.lock().await;

        for pr_num in removed {
            let branch = match state_guard.get(pr_num) {
                Some(s) => s.branch_name.clone(),
                None => continue,
            };

            let parent_branch = branch
                .as_str()
                .rsplit_once('.')
                .map(|(parent, _)| parent)
                .unwrap_or("main");

            for (sib_num, sib_state) in state_guard.iter() {
                if sib_num == pr_num {
                    continue;
                }
                let sib_parent = sib_state
                    .branch_name
                    .as_str()
                    .rsplit_once('.')
                    .map(|(p, _)| p)
                    .unwrap_or("main");
                if sib_parent == parent_branch
                    && registry.prs.contains_key(sib_num)
                {
                    let payload = serde_json::json!({
                        "merged_branch": branch.as_str(),
                        "parent_branch": parent_branch,
                        "sibling_pr_number": sib_num,
                    });
                    if let Ok(Some(action)) = self
                        .call_handle_event(
                            sib_state.branch_name.as_str(),
                            sib_state.agent_type,
                            "sibling_merged",
                            payload,
                        )
                        .await
                    {
                        self.handle_event_action(
                            action,
                            sib_state.branch_name.as_str(),
                            sib_state.agent_type,
                        )
                        .await;
                    }
                }
            }

            tracing::info!(
                otel.name = "agent.sibling_merged",
                agent_id = %branch,
                pr_number = *pr_num,
                branch = %branch,
                parent = %parent_branch,
                "[event] agent.sibling_merged"
            );
            if let Some(log) = self.ctx.event_log() {
                let _ = log.append(
                    "agent.sibling_merged",
                    branch.as_str(),
                    &serde_json::json!({
                        "pr_number": *pr_num,
                        "branch": branch.as_str(),
                        "parent": parent_branch,
                    }),
                );
            }
        }

        Ok(())
    }

    #[instrument(skip_all, fields(branch = %branch, event_type = %event_type))]
    async fn call_handle_event(
        &self,
        branch: &str,
        agent_type: AgentType,
        event_type: &str,
        payload: serde_json::Value,
    ) -> Result<Option<EventActionResponse>> {
        let plugins = match &self.plugins {
            Some(p) => p,
            None => return Ok(None),
        };

        let agent_name = branch.rsplit_once('.').map(|(_, s)| s).unwrap_or(branch);
        let role = match agent_type {
            AgentType::Claude => "tl",
            AgentType::Gemini => "dev",
            AgentType::Shoal => "dev",
            AgentType::OpenCode => "dev",
            AgentType::Process => return Ok(None),
        };

        let event_input = serde_json::json!({
            "role": role,
            "event_type": event_type,
            "payload": payload,
        });

        let plugins_guard = plugins.read().await;
        let plugin = match plugins_guard.get(&AgentName::from(agent_name)) {
            Some(p) => p.clone(),
            None => {
                info!(
                    "No plugin found for agent '{}', skipping event dispatch",
                    agent_name
                );
                return Ok(None);
            }
        };
        drop(plugins_guard);

        info!(
            "[EventDispatch] Calling handle_event for agent '{}': event_type={}, pr_payload={}",
            agent_name, event_type, payload
        );

        match plugin
            .call::<serde_json::Value, EventActionResponse>("handle_event", &event_input)
            .await
        {
            Ok(action) => {
                info!("[EventDispatch] handle_event returned: {:?}", action);
                let action_str = match action {
                    EventActionResponse::InjectMessage { .. } => "inject_message",
                    EventActionResponse::NotifyParent { .. } => "notify_parent",
                    EventActionResponse::NoAction => "no_action",
                };

                tracing::info!(
                    otel.name = "event.dispatched",
                    agent_id = %agent_name,
                    event_type = %event_type,
                    action = %action_str,
                    "[event] event.dispatched"
                );
                if let Some(log) = self.ctx.event_log() {
                    let _ = log.append(
                        "event.dispatched",
                        agent_name,
                        &serde_json::json!({
                            "event_type": event_type,
                            "action": action_str,
                        }),
                    );
                }

                Ok(Some(action))
            }
            Err(e) => {
                warn!(
                    "[EventDispatch] handle_event failed for {}: {}",
                    agent_name, e
                );

                tracing::info!(
                    otel.name = "event.dispatch_failed",
                    agent_id = %agent_name,
                    event_type = %event_type,
                    error = %e,
                    "[event] event.dispatch_failed"
                );
                if let Some(log) = self.ctx.event_log() {
                    let _ = log.append(
                        "event.dispatch_failed",
                        agent_name,
                        &serde_json::json!({
                            "event_type": event_type,
                            "error": e.to_string(),
                        }),
                    );
                }

                Ok(None)
            }
        }
    }

    async fn handle_event_action(
        &self,
        action: EventActionResponse,
        branch: &str,
        agent_type: AgentType,
    ) {
        match action {
            EventActionResponse::InjectMessage { message } => {
                let agent_name = AgentName::from(branch);
                let tab_name = if let Ok(records) = self.ctx.agent_resolver().records_ref().try_read() {
                    records.get(&agent_name).map(|r| r.display_name.clone())
                } else {
                    None
                }
                .unwrap_or_else(|| {
                    let slug = branch.rsplit_once('.').map(|(_, s)| s).unwrap_or(branch);
                    agent_type.tab_display_name(slug)
                });
                crate::services::delivery::deliver_to_agent(
                    &*self.ctx,
                    branch,
                    &tab_name,
                    &AgentName::from("event-handler"),
                    &message,
                    "Event handler action",
                )
                .await;
            }
            EventActionResponse::NotifyParent { message, pr_number: _pr_number } => {
                let agent_slug = branch.rsplit_once('.').map(|(_, s)| s).unwrap_or(branch);
                let parent_session_id = branch
                    .rsplit_once('.')
                    .map(|(parent, _)| parent.to_string())
                    .unwrap_or_else(|| "root".to_string());
                let parent_name = AgentName::from(parent_session_id.as_str());
                let parent_tab = crate::services::delivery::resolve_tab_name_for_agent(
                    &parent_name,
                    Some(self.ctx.agent_resolver()),
                );

                let agent_name = AgentName::from(agent_slug);
                crate::services::delivery::notify_parent_delivery(
                    &*self.ctx,
                    &agent_name,
                    &parent_session_id,
                    &parent_tab,
                    crate::services::delivery::NotifyStatus::Success,
                    &message,
                    None,
                    "event_handler",
                )
                .await;
            }
            EventActionResponse::NoAction => {}
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn emit_event(
        &self,
        branch: &str,
        status: &str,
        message: &str,
        _agent_type: AgentType,
        comments: Option<Vec<LocalReviewComment>>,
        reviews: Option<Vec<LocalReview>>,
    ) {
        info!(
            "Emitting event for branch {}: {} - {}",
            branch, status, message
        );

        let event_name = match status {
            "copilot_review" => "copilot.review",
            "success" | "failure" | "pending" => "ci.status_changed",
            other => other,
        };

        let comments_json = comments
            .as_ref()
            .and_then(|c| serde_json::to_string(c).ok())
            .unwrap_or_default();
        let reviews_json = reviews
            .as_ref()
            .and_then(|r| serde_json::to_string(r).ok())
            .unwrap_or_default();

        tracing::info!(
            otel.name = event_name,
            agent_id = %branch,
            branch = %branch,
            status = %status,
            message = %message,
            comments = %comments_json,
            reviews = %reviews_json,
            "[event] {}",
            event_name
        );
        if let Some(log) = self.ctx.event_log() {
            let _ = log.append(
                event_name,
                branch,
                &serde_json::json!({
                    "branch": branch,
                    "status": status,
                    "message": message,
                    "comments": comments,
                    "reviews": reviews,
                }),
            );
        }

        let event = Event {
            event_id: 0,
            event_type: Some(EventType::AgentMessage(AgentMessage {
                agent_id: branch.to_string(),
                status: status.to_string(),
                message: message.to_string(),
                changes: vec![],
            })),
        };
        self.ctx.event_queue().notify_event(branch, event).await;
    }

    async fn read_review_file(project_dir: &std::path::Path, pr_number: u64) -> Option<ReviewFile> {
        let path = project_dir
            .join(".exo/reviews")
            .join(format!("pr_{}.json", pr_number));
        match tokio::fs::read_to_string(&path).await {
            Ok(contents) => serde_json::from_str(&contents).ok(),
            Err(_) => None,
        }
    }
}

/// Pure state machine: given old state + new observations, compute pending actions.
#[allow(clippy::too_many_arguments)]
fn compute_pr_actions(
    old_state: &mut WatchState,
    pr_number: PRNumber,
    pr_sha: &str,
    comments: &[LocalReviewComment],
    reviews: &[LocalReview],
    ci_status: CIStatus,
    branch: &str,
    format_message: &dyn Fn(&[LocalReviewComment], &[LocalReview]) -> String,
) -> Vec<PendingAction> {
    let mut pending_actions = Vec::new();
    let comment_count = comments.len() + reviews.len();

    if old_state.notified_parent_approved || old_state.notified_parent_timeout {
        return pending_actions;
    }

    if pr_sha != old_state.last_sha {
        old_state.last_sha = pr_sha.to_string();
        if old_state.last_review_state == ReviewState::ChangesRequested {
            old_state.last_review_state = ReviewState::None;
            old_state.notified_parent_timeout = false;
            old_state.first_seen = Instant::now();
            old_state.addressed_changes = true;

            pending_actions.push(PendingAction::WasmEvent {
                event_type: "pr_review",
                payload: serde_json::json!({
                    "kind": "fixes_pushed",
                    "pr_number": pr_number.as_u64(),
                    "ci_status": ci_status.as_str(),
                }),
            });
        } else {
            pending_actions.push(PendingAction::WasmEvent {
                event_type: "pr_review",
                payload: serde_json::json!({
                    "kind": "commits_pushed",
                    "pr_number": pr_number.as_u64(),
                    "ci_status": ci_status.as_str(),
                }),
            });
        }
    }

    if comment_count != old_state.last_comment_count {
        if comment_count > old_state.last_comment_count {
            let message = format_message(comments, reviews);
            pending_actions.push(PendingAction::EmitEvent {
                status: "copilot_review".to_string(),
                message: message.clone(),
                comments: Some(comments.to_vec()),
                reviews: Some(reviews.to_vec()),
            });

            pending_actions.push(PendingAction::WasmEvent {
                event_type: "pr_review",
                payload: serde_json::json!({
                    "kind": "review_received",
                    "pr_number": pr_number.as_u64(),
                    "comments": message,
                }),
            });
        }
        old_state.last_comment_count = comment_count;
    }

    let approved = reviews
        .iter()
        .any(|r| r.state == ReviewState::Approved || r.body.to_lowercase().contains("approved"));
    if approved && old_state.last_review_state != ReviewState::Approved {
        old_state.last_review_state = ReviewState::Approved;
        old_state.notified_parent_approved = true;
        pending_actions.push(PendingAction::WasmEvent {
            event_type: "pr_review",
            payload: serde_json::json!({
                "kind": "approved",
                "pr_number": pr_number.as_u64(),
            }),
        });
    }

    let changes_requested = reviews
        .iter()
        .any(|r| r.state == ReviewState::ChangesRequested);
    if changes_requested && old_state.last_review_state != ReviewState::ChangesRequested {
        old_state.last_review_state = ReviewState::ChangesRequested;
        pending_actions.push(PendingAction::WasmEvent {
            event_type: "pr_review",
            payload: serde_json::json!({
                "kind": "review_received",
                "pr_number": pr_number.as_u64(),
                "comments": format_message(comments, reviews),
            }),
        });
    }

    if ci_status != old_state.last_ci_status {
        pending_actions.push(PendingAction::WasmEvent {
            event_type: "ci_status",
            payload: serde_json::json!({
                "pr_number": pr_number.as_u64(),
                "status": ci_status.as_str(),
                "branch": branch,
            }),
        });
        pending_actions.push(PendingAction::EmitEvent {
            status: ci_status.to_string(),
            message: format!("[CI STATUS: {}] {}", branch, ci_status),
            comments: None,
            reviews: None,
        });
        old_state.last_ci_status = ci_status;
    }

    let timeout_minutes: u64 = if old_state.addressed_changes { 5 } else { 15 };
    if !old_state.notified_parent_timeout
        && old_state.last_review_state == ReviewState::None
        && !old_state.notified_parent_approved
        && old_state.first_seen.elapsed() > Duration::from_secs(timeout_minutes * 60)
    {
        old_state.notified_parent_timeout = true;
        pending_actions.push(PendingAction::WasmEvent {
            event_type: "pr_review",
            payload: serde_json::json!({
                "kind": "timeout",
                "pr_number": pr_number.as_u64(),
                "minutes_elapsed": timeout_minutes,
            }),
        });
    }

    pending_actions
}

fn obs_to_review_parts(obs: &Observation) -> (Vec<LocalReview>, ReviewState) {
    let state = match obs.review_state {
        LocalReviewState::Approved => ReviewState::Approved,
        LocalReviewState::ChangesRequested => ReviewState::ChangesRequested,
        LocalReviewState::PendingReview => ReviewState::None,
    };

    let mut reviews: Vec<LocalReview> = obs
        .comments
        .iter()
        .map(|c| LocalReview {
            body: c.body.clone(),
            state: state.clone(),
        })
        .collect();

    if obs.review_state == LocalReviewState::Approved && reviews.is_empty() {
        reviews.push(LocalReview {
            body: "Approved".to_string(),
            state: ReviewState::Approved,
        });
    } else if obs.review_state == LocalReviewState::ChangesRequested && reviews.is_empty() {
        reviews.push(LocalReview {
            body: "Changes requested".to_string(),
            state: ReviewState::ChangesRequested,
        });
    }

    (reviews, state)
}

fn format_review_message(comments: &[LocalReviewComment], reviews: &[LocalReview]) -> String {
    let mut msg = String::new();

    if !reviews.is_empty() {
        let review_bodies: Vec<&str> = reviews
            .iter()
            .filter(|r| !r.body.is_empty())
            .map(|r| r.body.as_str())
            .collect();
        if !review_bodies.is_empty() {
            msg.push_str("Review summary:\n");
            for body in review_bodies {
                msg.push_str(body);
                msg.push('\n');
            }
        }
    }

    if !comments.is_empty() {
        if !msg.is_empty() {
            msg.push('\n');
        }
        msg.push_str("Inline comments:\n");
        for (i, c) in comments.iter().enumerate() {
            let file_label = c.path.as_deref().unwrap_or("unknown file");
            msg.push_str(&format!("{}. [{}] {}\n", i + 1, file_label, c.body));
            if let Some(ref hunk) = c.diff_hunk {
                msg.push_str(&format!("   ```diff\n   {}\n   ```\n", hunk));
            }
        }
    }

    if msg.is_empty() {
        msg.push_str("Review activity detected (no body text)");
    }

    msg
}

async fn git_head_sha(worktree_path: &std::path::Path) -> Result<String> {
    if !worktree_path.exists() {
        return Ok(String::new());
    }
    let output = Command::new("git")
        .arg("-C")
        .arg(worktree_path)
        .args(["rev-parse", "HEAD"])
        .output()
        .await?;

    if output.status.success() {
        let sha = String::from_utf8_lossy(&output.stdout).trim().to_string();
        Ok(sha)
    } else {
        Ok(String::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_branch(name: &str, _at: AgentType) -> BranchName {
        BranchName::from(name)
    }

    fn test_state(branch: &BranchName, agent_type: AgentType, sha: &str) -> WatchState {
        WatchState::new(branch, agent_type, sha, CIStatus::Unknown, 0)
    }

    fn test_comment(body: &str) -> LocalReviewComment {
        LocalReviewComment {
            body: body.to_string(),
            path: None,
            diff_hunk: None,
        }
    }

    fn test_review(body: &str, state: ReviewState) -> LocalReview {
        LocalReview {
            body: body.to_string(),
            state,
        }
    }

    // ---------------------------------------------------------------------------
    // compute_pr_actions tests
    // ---------------------------------------------------------------------------

    #[test]
    fn test_new_sha_fires_commits_pushed() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "def456",
            &[],
            &[],
            CIStatus::Unknown,
            branch.as_str(),
            &|_, _| String::new(),
        );
        assert!(actions.iter().any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "commits_pushed")));
        assert_eq!(state.last_sha, "def456");
    }

    #[test]
    fn test_sha_change_after_changes_requested_fires_fixes_pushed() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.last_review_state = ReviewState::ChangesRequested;
        state.addressed_changes = false;

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "def456",
            &[],
            &[],
            CIStatus::Unknown,
            branch.as_str(),
            &|_, _| String::new(),
        );
        assert!(actions.iter().any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "fixes_pushed")));
        assert!(state.addressed_changes);
        assert_eq!(state.last_review_state, ReviewState::None);
    }

    #[test]
    fn test_new_comments_fire_review_received() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let comments = vec![test_comment("Fix this")];
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &comments,
            &[],
            CIStatus::Unknown,
            branch.as_str(),
            &|_, _| "review message".to_string(),
        );
        assert!(actions.iter().any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "review_received")));
        assert_eq!(state.last_comment_count, 1);
    }

    #[test]
    fn test_approval_fires_approved() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review("LGTM!", ReviewState::Approved)];
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &reviews,
            CIStatus::Unknown,
            branch.as_str(),
            &|_, _| String::new(),
        );
        assert!(actions.iter().any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "approved")));
        assert!(state.notified_parent_approved);
    }

    #[test]
    fn test_changes_requested_fires_review_received() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review("Needs work", ReviewState::ChangesRequested)];
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &reviews,
            CIStatus::Unknown,
            branch.as_str(),
            &|_, _| String::new(),
        );
        assert_eq!(state.last_review_state, ReviewState::ChangesRequested);
        assert!(actions.iter().any(|a| matches!(a, PendingAction::WasmEvent { event_type: "pr_review", .. })));
    }

    #[test]
    fn test_ci_change_fires_event() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.last_ci_status = CIStatus::Pending;
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Success,
            branch.as_str(),
            &|_, _| String::new(),
        );
        assert!(actions.iter().any(|a| matches!(a, PendingAction::WasmEvent { event_type: "ci_status", .. })));
        assert_eq!(state.last_ci_status, CIStatus::Success);
    }

    #[test]
    fn test_timeout_after_15_minutes() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.first_seen = Instant::now() - Duration::from_secs(16 * 60);
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Unknown,
            branch.as_str(),
            &|_, _| String::new(),
        );
        assert!(actions.iter().any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "timeout")));
        assert!(state.notified_parent_timeout);
    }

    #[test]
    fn test_stale_guard_suppresses_after_approval() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.notified_parent_approved = true;
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "def456",
            &[test_comment("late")],
            &[test_review("late review", ReviewState::Approved)],
            CIStatus::Success,
            branch.as_str(),
            &|_, _| String::new(),
        );
        assert!(actions.is_empty());
    }

    #[test]
    fn test_no_duplicate_approval() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.last_review_state = ReviewState::Approved;
        state.notified_parent_approved = true;
        let reviews = vec![test_review("Still approved", ReviewState::Approved)];
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &reviews,
            CIStatus::Unknown,
            branch.as_str(),
            &|_, _| String::new(),
        );
        assert!(actions.is_empty());
    }

    #[test]
    fn test_approval_detected_from_body_text() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![LocalReview {
            body: "I have reviewed this and it is APPROVED".to_string(),
            state: ReviewState::None,
        }];
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &reviews,
            CIStatus::Unknown,
            branch.as_str(),
            &|_, _| String::new(),
        );
        assert!(actions.iter().any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "approved")));
    }

    #[test]
    fn test_timeout_shorter_after_addressed_changes() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.addressed_changes = true;
        state.first_seen = Instant::now() - Duration::from_secs(6 * 60);
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Unknown,
            branch.as_str(),
            &|_, _| String::new(),
        );
        assert!(actions.iter().any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "timeout" && payload["minutes_elapsed"] == 5)));
    }

    #[test]
    fn test_no_ci_event_when_status_unchanged() {
        let branch = BranchName::from("main.feat-gemini");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.last_ci_status = CIStatus::Success;
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Success,
            branch.as_str(),
            &|_, _| String::new(),
        );
        assert!(actions.is_empty());
    }

    // ---------------------------------------------------------------------------
    // obs_to_review_parts tests
    // ---------------------------------------------------------------------------

    #[test]
    fn test_obs_to_review_parts_pending() {
        let obs = Observation {
            head_sha: "abc".into(),
            review_state: LocalReviewState::PendingReview,
            comments: vec![],
            ci_status: CIStatus::Unknown,
        };
        let (reviews, state) = obs_to_review_parts(&obs);
        assert_eq!(state, ReviewState::None);
        assert!(reviews.is_empty());
    }

    #[test]
    fn test_obs_to_review_parts_approved_with_no_comments_creates_synthetic() {
        let obs = Observation {
            head_sha: "abc".into(),
            review_state: LocalReviewState::Approved,
            comments: vec![],
            ci_status: CIStatus::Unknown,
        };
        let (reviews, state) = obs_to_review_parts(&obs);
        assert_eq!(state, ReviewState::Approved);
        assert!(reviews.iter().any(|r| r.state == ReviewState::Approved));
    }

    #[test]
    fn test_obs_to_review_parts_changes_requested() {
        let obs = Observation {
            head_sha: "abc".into(),
            review_state: LocalReviewState::ChangesRequested,
            comments: vec![],
            ci_status: CIStatus::Unknown,
        };
        let (reviews, state) = obs_to_review_parts(&obs);
        assert_eq!(state, ReviewState::ChangesRequested);
        assert!(reviews.iter().any(|r| r.state == ReviewState::ChangesRequested));
    }

    // ---------------------------------------------------------------------------
    // format_review_message tests
    // ---------------------------------------------------------------------------

    #[test]
    fn test_format_message_empty() {
        let msg = format_review_message(&[], &[]);
        assert_eq!(msg, "Review activity detected (no body text)");
    }

    #[test]
    fn test_format_message_with_reviews() {
        let reviews = vec![
            LocalReview { body: "LGTM!".to_string(), state: ReviewState::Approved },
            LocalReview { body: "Good work.".to_string(), state: ReviewState::None },
        ];
        let msg = format_review_message(&[], &reviews);
        assert!(msg.contains("Review summary:"));
        assert!(msg.contains("LGTM!"));
        assert!(msg.contains("Good work."));
    }

    #[test]
    fn test_format_message_with_inline_comments() {
        let comments = vec![
            LocalReviewComment {
                body: "Fix this typo".to_string(),
                path: Some("src/main.rs".to_string()),
                diff_hunk: Some("@@ -1,3 +1,3 @@".to_string()),
            },
        ];
        let msg = format_review_message(&comments, &[]);
        assert!(msg.contains("Inline comments:"));
        assert!(msg.contains("Fix this typo"));
        assert!(msg.contains("src/main.rs"));
        assert!(msg.contains("```diff"));
    }

    // ---------------------------------------------------------------------------
    // WatchState tests
    // ---------------------------------------------------------------------------

    #[test]
    fn test_watch_state_new_sets_defaults() {
        let branch = BranchName::from("main.feat-gemini");
        let state = WatchState::new(&branch, AgentType::Gemini, "abc123", CIStatus::Unknown, 0);
        assert_eq!(state.branch_name.as_str(), "main.feat-gemini");
        assert_eq!(state.last_sha, "abc123");
        assert_eq!(state.last_review_state, ReviewState::None);
        assert!(!state.notified_parent_approved);
        assert!(!state.notified_parent_timeout);
        assert!(!state.addressed_changes);
    }

    // ---------------------------------------------------------------------------
    // ReviewFile deserialization
    // ---------------------------------------------------------------------------

    #[test]
    fn test_review_file_deserializes() {
        let json = r#"{"state": "approved", "comments": []}"#;
        let rf: ReviewFile = serde_json::from_str(json).unwrap();
        assert_eq!(rf.state, "approved");
        assert!(rf.comments.is_empty());
    }

    #[test]
    fn test_review_file_defaults_missing_state() {
        let json = r#"{}"#;
        let rf: ReviewFile = serde_json::from_str(json).unwrap();
        assert_eq!(rf.state, "");
    }

    #[test]
    fn test_review_file_with_comments() {
        let json = r#"{
            "state": "changes_requested",
            "comments": [
                {"body": "Needs more tests", "path": "src/lib.rs", "line": 42}
            ]
        }"#;
        let rf: ReviewFile = serde_json::from_str(json).unwrap();
        assert_eq!(rf.state, "changes_requested");
        assert_eq!(rf.comments.len(), 1);
        assert_eq!(rf.comments[0].body, "Needs more tests");
    }
}
