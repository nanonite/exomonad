use crate::domain::{AgentName, BirthBranch, BranchName, CIStatus, PRNumber};
use crate::plugin_manager::PluginManager;
use crate::services::agent_control::AgentType;
use crate::services::file_pr_local::{
    read_pr_registry, write_pr_registry, LocalReviewState, PrRegistry, PrState,
};
use crate::services::review_policy::ReviewPolicy;
use crate::services::{
    HasAcpRegistry, HasAgentResolver, HasEventLog, HasEventQueue, HasProjectDir, HasTeamRegistry,
    ReviewerSpawner,
};
use anyhow::{Context, Result};
use exomonad_proto::effects::events::{event::EventType, AgentMessage, Event};
use futures::StreamExt;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::process::Command;
use tokio::sync::{mpsc, Mutex, RwLock};
use tracing::{debug, info, instrument, warn};
use url::Url;

type PluginMap = Arc<RwLock<HashMap<AgentName, Arc<PluginManager>>>>;
const MERGE_READY_SIGNAL_WINDOW: Duration = Duration::from_secs(30 * 60);

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
    thread_id: Option<String>,
    resolved: bool,
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
    WriteRegistryStuck {
        pr_number: u64,
        rounds: u32,
    },
}

struct PendingPrActions {
    pr_number: u64,
    actions: Vec<PendingAction>,
    branch: BranchName,
    agent_type: AgentType,
    agent_name: String,
    agent_role: String,
}

/// Whether a PR review event should fan out to the reviewer in addition to the leaf,
/// and the reviewer's identity when so.
///
/// The reviewer-side handlers in `.exo/roles/devswarm/ReviewerRole.hs` are only reachable
/// when the watcher explicitly dispatches the event to the reviewer's plugin manager.
/// This decision is computed by [`reviewer_fanout_decision`] and consumed by the
/// dispatch loop in [`AgentControlService::process_observations`].
#[derive(Debug, PartialEq, Eq)]
enum ReviewerFanOut {
    /// Event doesn't require reviewer fan-out (most events).
    NotApplicable,
    /// Event requires fan-out, but the PR has no registered reviewer — the
    /// convergence loop will stall. Caller logs an error.
    NoReviewer,
    /// Event requires fan-out and the reviewer is registered.
    DispatchTo(BranchName, AgentType, &'static str),
}

fn reviewer_worktree_path(project_dir: &Path, reviewer_agent: &str) -> PathBuf {
    project_dir.join(".exo/worktrees").join(reviewer_agent)
}

/// Decide whether to fan a PR review event out to the reviewer.
///
/// Every `pr_review` event kind currently emitted by the watcher has a corresponding
/// handler in `.exo/roles/devswarm/ReviewerRole.hs` (`fixes_pushed`, `commits_pushed`,
/// `review_received`, `approved`, `timeout`, `reviewer_approved`,
/// `reviewer_requested_changes`, `rate_limited`, `stuck`, `merge_ready`), so the
/// fan-out condition is the event_type alone — the Haskell handler decides whether to
/// act on its own kind (some kinds the reviewer caused; the handler returns NoAction).
///
/// Non-`pr_review` event_types (`ci_status`, `agent.sibling_merged`, etc.) remain
/// leaf-only because the reviewer has no handler for them.
fn reviewer_fanout_decision(
    event_type: &str,
    _payload: &serde_json::Value,
    pr_number: u64,
    registry: &PrRegistry,
) -> ReviewerFanOut {
    if event_type != "pr_review" {
        return ReviewerFanOut::NotApplicable;
    }
    match registry.reviewer_for_pr(pr_number) {
        Some((branch, agent_type)) => ReviewerFanOut::DispatchTo(branch, agent_type, "reviewer"),
        None => ReviewerFanOut::NoReviewer,
    }
}

fn legacy_event_role_for_agent_type(agent_type: AgentType) -> &'static str {
    match agent_type {
        AgentType::Claude => "tl",
        AgentType::Gemini | AgentType::Shoal | AgentType::OpenCode | AgentType::Codex => "dev",
        AgentType::Process => "process",
    }
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
    rounds: u32,
    stuck: bool,
    reviewer_spawned: bool,
    review_approved_at: Option<Instant>,
    ci_mergeable_at: Option<Instant>,
    merge_ready_notified: bool,
}

impl WatchState {
    fn new(
        branch: &BranchName,
        agent_type: AgentType,
        sha: &str,
        ci_status: CIStatus,
        comment_count: usize,
    ) -> Self {
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
            rounds: 0,
            stuck: false,
            reviewer_spawned: false,
            review_approved_at: None,
            ci_mergeable_at: if matches!(ci_status, CIStatus::Success | CIStatus::Neutral) {
                Some(Instant::now())
            } else {
                None
            },
            merge_ready_notified: false,
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
    diff_hunk: Option<String>,
    #[serde(default)]
    thread_id: Option<String>,
    #[serde(default)]
    resolved: bool,
}

/// Observation collected from local sources for one open PR.
struct Observation {
    head_sha: String,
    review_state: LocalReviewState,
    comments: Vec<LocalReviewComment>,
    ci_status: CIStatus,
}

#[derive(Debug, Clone)]
struct PipelineContext {
    branch_name: BranchName,
    pr_number: Option<u64>,
    agent_name: Option<String>,
}

#[derive(Debug, Clone, Copy)]
enum CiSubscriberKind {
    Knot,
    Spindle,
}

/// Replaces `github_poller.rs` and `copilot_review.rs` by observing the local
/// `.exo/prs.json` registry, `.exo/reviews/` files, and git worktree state.
pub struct WorktreeEventWatcher<C> {
    ctx: Arc<C>,
    poll_interval: Duration,
    state: Arc<Mutex<HashMap<u64, WatchState>>>,
    prs_path: std::path::PathBuf,
    plugins: Option<PluginMap>,
    policy: ReviewPolicy,
    /// Shared CI status map updated by the spindle subscriber (branch → CIStatus).
    ci_status_map: Arc<RwLock<HashMap<BranchName, CIStatus>>>,
    /// WebSocket URL of the Tangled knot (for pipeline→branch mapping).
    knot_url: Option<String>,
    /// WebSocket URL of the Tangled spindle (for PipelineStatus events).
    spindle_url: Option<String>,
    /// Spawns reviewer agents on PR creation.
    reviewer_spawner: Option<Arc<dyn ReviewerSpawner>>,
}

impl<C> WorktreeEventWatcher<C>
where
    C: HasTeamRegistry
        + HasAcpRegistry
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
            policy: ReviewPolicy::default(),
            ci_status_map: Arc::new(RwLock::new(HashMap::new())),
            knot_url: None,
            spindle_url: None,
            reviewer_spawner: None,
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

    pub fn with_policy(mut self, policy: ReviewPolicy) -> Self {
        self.policy = policy;
        self
    }

    pub fn with_knot_url(mut self, url: String) -> Self {
        let normalized = normalize_tangled_event_ws_url(&url);
        if normalized != url {
            info!(configured_url = %url, subscriber_url = %normalized, "Normalized Tangled knot event stream URL");
        }
        self.knot_url = Some(normalized);
        self
    }

    pub fn with_spindle_url(mut self, url: String) -> Self {
        let normalized = normalize_tangled_event_ws_url(&url);
        if normalized != url {
            info!(configured_url = %url, subscriber_url = %normalized, "Normalized Tangled spindle event stream URL");
        }
        self.spindle_url = Some(normalized);
        self
    }

    pub fn with_reviewer_spawner(mut self, spawner: Arc<dyn ReviewerSpawner>) -> Self {
        self.reviewer_spawner = Some(spawner);
        self
    }

    /// Use a shared CI status map (e.g. from `Services`) instead of the internal one.
    ///
    /// Call this so the merge handler and the watcher read from the same map.
    pub fn with_ci_status_map(mut self, map: Arc<RwLock<HashMap<BranchName, CIStatus>>>) -> Self {
        self.ci_status_map = map;
        self
    }

    pub async fn run(&self) {
        tracing::info!(
            poll_interval_secs = self.poll_interval.as_secs(),
            "Local worktree event watcher started"
        );

        // Launch CI event subscriber if either URL is configured
        if self.knot_url.is_some() || self.spindle_url.is_some() {
            let ci_map = self.ci_status_map.clone();
            let knot_url = self.knot_url.clone();
            let spindle_url = self.spindle_url.clone();
            let project_dir = self.ctx.project_dir();
            let worktrees_dir = project_dir.join(".exo/worktrees");
            let repo_name = project_dir
                .file_name()
                .and_then(|name| name.to_str())
                .map(|name| name.to_string());
            tokio::spawn(async move {
                run_ci_subscriber(knot_url, spindle_url, ci_map, worktrees_dir, repo_name).await;
            });
        }

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
                        info!(
                            previous_failures = consecutive_failures,
                            "Watcher recovered"
                        );
                    }
                    consecutive_failures = 0;
                }
                Err(e) => {
                    consecutive_failures += 1;
                    let next_retry_secs = {
                        let backoff =
                            base_interval * 2u32.saturating_pow(consecutive_failures.min(6));
                        backoff.min(max_backoff).as_secs()
                    };
                    if consecutive_failures <= 3 {
                        warn!(
                            consecutive_failures,
                            next_retry_secs, "Watcher cycle failed: {}", e
                        );
                    } else {
                        debug!(
                            consecutive_failures,
                            next_retry_secs, "Watcher cycle failed: {}", e
                        );
                    }
                }
            }
        }
    }

    async fn set_pr_stuck(&self, pr_number: u64, rounds: u32) -> anyhow::Result<()> {
        let mut registry: PrRegistry = read_pr_registry(&self.prs_path).await?;
        if let Some(pr) = registry.prs.get_mut(&pr_number) {
            pr.stuck = true;
            pr.rounds = rounds;
            write_pr_registry(&self.prs_path, &registry).await?;
            info!(pr_number, rounds, "Set stuck flag on PR");
        }
        Ok(())
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

    async fn collect_observations(
        &self,
        registry: &crate::services::file_pr_local::PrRegistry,
    ) -> Result<HashMap<u64, Observation>> {
        let mut observations = HashMap::new();
        let project_dir = self.ctx.project_dir().to_path_buf();

        for (number, pr) in &registry.prs {
            if pr.state != PrState::Open {
                continue;
            }

            let worktree_path = project_dir.join(".exo/worktrees").join(&pr.author_agent);

            let head_sha = git_head_sha(&worktree_path).await.unwrap_or_default();

            let review_file = Self::read_review_file(&project_dir, *number).await;
            let (review_state, comments) = match review_file {
                Some(rf) => {
                    let state = match rf.state.as_str() {
                        "approved" => LocalReviewState::Approved,
                        "changes_requested" => LocalReviewState::ChangesRequested,
                        _ => LocalReviewState::PendingReview,
                    };
                    let lrc: Vec<LocalReviewComment> = rf
                        .comments
                        .into_iter()
                        .map(|c| LocalReviewComment {
                            body: c.body,
                            path: c.path,
                            diff_hunk: c.diff_hunk,
                            thread_id: c.thread_id,
                            resolved: c.resolved,
                        })
                        .collect();
                    (state, lrc)
                }
                None => {
                    let state = pr.review_state.clone();
                    (state, vec![])
                }
            };

            let ci_status = {
                let branch = BranchName::try_from_str(pr.head_branch.as_str())
                    .expect("validated string input is non-empty");
                self.ci_status_map
                    .read()
                    .await
                    .get(&branch)
                    .copied()
                    .unwrap_or(CIStatus::Unknown)
            };

            observations.insert(
                *number,
                Observation {
                    head_sha,
                    review_state,
                    comments,
                    ci_status,
                },
            );
        }

        Ok(observations)
    }

    async fn process_observations(
        &self,
        registry: &crate::services::file_pr_local::PrRegistry,
        observations: &HashMap<u64, Observation>,
    ) -> Result<Vec<u64>> {
        let mut removed_prs = Vec::new();
        let mut pending_actions: Vec<PendingPrActions> = Vec::new();
        let mut head_sha_updates: Vec<(u64, String)> = Vec::new();

        {
            let mut state_guard = self.state.lock().await;

            for (pr_number, obs) in observations {
                let pr = match registry.prs.get(pr_number) {
                    Some(p) => p,
                    None => continue,
                };

                let agent_name = &pr.author_agent;
                let agent_type = AgentType::from_dir_name(agent_name);
                let agent_role = pr.author_role.clone();
                let branch = BranchName::try_from_str(pr.head_branch.as_str())
                    .expect("validated string input is non-empty");
                let (local_reviews, _local_review_state) = obs_to_review_parts(obs);
                if pr.last_head_sha.as_deref() != Some(obs.head_sha.as_str()) {
                    head_sha_updates.push((*pr_number, obs.head_sha.clone()));
                }

                let actions = if let Some(old_state) = state_guard.get_mut(pr_number) {
                    compute_pr_actions(
                        old_state,
                        PRNumber::new(*pr_number),
                        &obs.head_sha,
                        &obs.comments,
                        &local_reviews,
                        obs.ci_status,
                        pr.merge_blocked_on_ci,
                        branch.as_str(),
                        &|c, r| format_review_message(c, r),
                        self.policy.reviewer_max_rounds,
                    )
                } else {
                    state_guard.insert(
                        *pr_number,
                        WatchState::new(&branch, agent_type, &obs.head_sha, CIStatus::Unknown, 0),
                    );
                    let actions = compute_pr_actions(
                        state_guard
                            .get_mut(pr_number)
                            .expect("watch state inserted above"),
                        PRNumber::new(*pr_number),
                        &obs.head_sha,
                        &obs.comments,
                        &local_reviews,
                        obs.ci_status,
                        pr.merge_blocked_on_ci,
                        branch.as_str(),
                        &|c, r| format_review_message(c, r),
                        self.policy.reviewer_max_rounds,
                    );
                    // Spawn reviewer immediately on first sighting of a new open PR
                    if let Some(spawner) = &self.reviewer_spawner {
                        if let Some(pr_entry) = registry.prs.get(pr_number) {
                            let spawner = spawner.clone();
                            let pr_clone = pr_entry.clone();
                            let pr_num = *pr_number;
                            tokio::spawn(async move {
                                info!(pr_number = pr_num, "Spawning reviewer agent for new PR");
                                match spawner.spawn_reviewer_for_pr(&pr_clone).await {
                                    Ok(_) => info!(
                                        pr_number = pr_num,
                                        "Reviewer agent spawned successfully"
                                    ),
                                    Err(e) => {
                                        warn!(pr_number = pr_num, error = %e, "Failed to spawn reviewer for PR")
                                    }
                                }
                            });
                            if let Some(ws) = state_guard.get_mut(pr_number) {
                                ws.reviewer_spawned = true;
                            }
                        }
                    }
                    actions
                };

                if !actions.is_empty() {
                    pending_actions.push(PendingPrActions {
                        pr_number: *pr_number,
                        actions,
                        branch,
                        agent_type,
                        agent_name: agent_name.clone(),
                        agent_role,
                    });
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

        if !head_sha_updates.is_empty() {
            self.persist_last_head_shas(&head_sha_updates).await?;
        }

        for pending in pending_actions {
            for action in pending.actions {
                match action {
                    PendingAction::WasmEvent {
                        event_type,
                        payload,
                    } => {
                        let release_message = merge_ready_release_message(&payload);
                        if event_type == "ci_status" {
                            info!(
                                pr_number = pending.pr_number,
                                agent_name = %pending.agent_name,
                                branch = %pending.branch,
                                status = payload.get("status").and_then(|value| value.as_str()).unwrap_or("unknown"),
                                "Local PR CI status event dispatching"
                            );
                        }

                        // Fan-out: every pr_review event has a reviewer-side handler in
                        // .exo/roles/devswarm/ReviewerRole.hs. Dispatch to the reviewer in
                        // addition to the leaf so the convergence loop progresses without
                        // TL intervention. See reviewer_fanout_decision for the rationale.
                        let fan_out_decision = reviewer_fanout_decision(
                            event_type,
                            &payload,
                            pending.pr_number,
                            registry,
                        );
                        let payload_kind = payload
                            .get("kind")
                            .and_then(|v| v.as_str())
                            .unwrap_or("<unknown>")
                            .to_string();
                        let reviewer_payload = match &fan_out_decision {
                            ReviewerFanOut::DispatchTo(_, _, _) => Some(payload.clone()),
                            _ => None,
                        };

                        if let Ok(Some(response)) = self
                            .call_handle_event_for_role(
                                pending.branch.as_str(),
                                pending.agent_type,
                                &pending.agent_role,
                                event_type,
                                payload,
                            )
                            .await
                        {
                            self.handle_event_action(
                                response,
                                pending.branch.as_str(),
                                pending.agent_type,
                            )
                            .await;
                            if let Some(message) = release_message {
                                self.deliver_release_message(
                                    pending.branch.as_str(),
                                    pending.agent_type,
                                    &message,
                                )
                                .await;
                            }
                        }

                        match fan_out_decision {
                            ReviewerFanOut::DispatchTo(
                                reviewer_branch,
                                reviewer_agent_type,
                                reviewer_role,
                            ) => {
                                if let Some(payload) = reviewer_payload {
                                    if let Err(err) = self
                                        .advance_reviewer_worktree_for_fixes(
                                            registry,
                                            pending.pr_number,
                                            &payload,
                                        )
                                        .await
                                    {
                                        warn!(
                                            pr_number = pending.pr_number,
                                            reviewer_branch = %reviewer_branch,
                                            error = %err,
                                            "Failed to advance reviewer worktree before pr_review fan-out"
                                        );
                                    }
                                    info!(
                                        pr_number = pending.pr_number,
                                        reviewer_branch = %reviewer_branch,
                                        ?reviewer_agent_type,
                                        reviewer_role,
                                        kind = %payload_kind,
                                        "Fanning out pr_review event to reviewer agent"
                                    );
                                    if let Ok(Some(response)) = self
                                        .call_handle_event_for_role(
                                            reviewer_branch.as_str(),
                                            reviewer_agent_type,
                                            &reviewer_role,
                                            event_type,
                                            payload,
                                        )
                                        .await
                                    {
                                        self.handle_event_action(
                                            response,
                                            reviewer_branch.as_str(),
                                            reviewer_agent_type,
                                        )
                                        .await;
                                    }
                                }
                            }
                            ReviewerFanOut::NoReviewer => {
                                tracing::error!(
                                    pr_number = pending.pr_number,
                                    leaf_branch = %pending.branch,
                                    kind = %payload_kind,
                                    "pr_review event fired but no reviewer is registered for \
                                     this PR — the leaf+reviewer convergence loop will \
                                     stall. Check that spawn_reviewer_for_pr ran for this \
                                     PR and that the PR registry was updated."
                                );
                            }
                            ReviewerFanOut::NotApplicable => {}
                        }
                    }
                    PendingAction::EmitEvent {
                        status,
                        message,
                        comments,
                        reviews,
                    } => {
                        self.emit_event(
                            pending.branch.as_str(),
                            &status,
                            &message,
                            pending.agent_type,
                            comments,
                            reviews,
                        )
                        .await;
                    }
                    PendingAction::WriteRegistryStuck { pr_number, rounds } => {
                        if let Err(e) = self.set_pr_stuck(pr_number, rounds).await {
                            warn!(pr_number, rounds, error = %e, "Failed to set stuck flag on PR");
                        }
                    }
                }
            }
        }

        Ok(removed_prs)
    }

    async fn persist_last_head_shas(&self, updates: &[(u64, String)]) -> Result<()> {
        let mut registry = read_pr_registry(&self.prs_path).await?;
        let mut changed = false;

        for (pr_number, head_sha) in updates {
            if let Some(pr) = registry.prs.get_mut(pr_number) {
                if pr.last_head_sha.as_deref() != Some(head_sha.as_str()) {
                    pr.last_head_sha = Some(head_sha.clone());
                    changed = true;
                }
            }
        }

        if changed {
            write_pr_registry(&self.prs_path, &registry).await?;
        }

        Ok(())
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
                if sib_parent == parent_branch && registry.prs.contains_key(sib_num) {
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
        let role = legacy_event_role_for_agent_type(agent_type);
        self.call_handle_event_for_role(branch, agent_type, role, event_type, payload)
            .await
    }

    #[instrument(skip_all, fields(branch = %branch, role = %role, event_type = %event_type))]
    async fn call_handle_event_for_role(
        &self,
        branch: &str,
        agent_type: AgentType,
        role: &str,
        event_type: &str,
        payload: serde_json::Value,
    ) -> Result<Option<EventActionResponse>> {
        let plugins = match &self.plugins {
            Some(p) => p,
            None => return Ok(None),
        };

        let agent_name = self.resolve_event_agent_name(branch, agent_type).await;
        if role == "process" {
            return Ok(None);
        }

        let event_input = serde_json::json!({
            "role": role,
            "event_type": event_type,
            "payload": payload,
        });

        let plugins_guard = plugins.read().await;
        let plugin = match plugins_guard.get(&agent_name) {
            Some(p) => p.clone(),
            None => {
                tracing::error!(
                    branch,
                    lookup_key = %agent_name,
                    ?agent_type,
                    role,
                    event_type,
                    "No plugin found for event target; skipping event dispatch"
                );
                return Ok(None);
            }
        };
        drop(plugins_guard);

        info!(
            "[EventDispatch] Calling handle_event for agent '{}': role={}, event_type={}, pr_payload={}",
            agent_name, role, event_type, payload
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
                    role = %role,
                    event_type = %event_type,
                    action = %action_str,
                    "[event] event.dispatched"
                );
                if let Some(log) = self.ctx.event_log() {
                    let _ = log.append(
                        "event.dispatched",
                        agent_name.as_str(),
                        &serde_json::json!({
                            "role": role,
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
                    role = %role,
                    event_type = %event_type,
                    error = %e,
                    "[event] event.dispatch_failed"
                );
                if let Some(log) = self.ctx.event_log() {
                    let _ = log.append(
                        "event.dispatch_failed",
                        agent_name.as_str(),
                        &serde_json::json!({
                            "role": role,
                            "event_type": event_type,
                            "error": e.to_string(),
                        }),
                    );
                }

                Ok(None)
            }
        }
    }

    async fn resolve_event_agent_name(&self, branch: &str, agent_type: AgentType) -> AgentName {
        let branch_tail = branch.rsplit_once('.').map(|(_, s)| s).unwrap_or(branch);
        let branch_name =
            BirthBranch::try_from_str(branch).expect("validated string input is non-empty");
        let records = self.ctx.agent_resolver().records_ref().read().await;

        if let Some(record) = records
            .values()
            .find(|record| record.birth_branch == branch_name && record.agent_type == agent_type)
        {
            return record.agent_name.clone();
        }

        let exact = AgentName::try_from_str(branch).expect("validated string input is non-empty");
        if records.contains_key(&exact) {
            return exact;
        }

        AgentName::try_from_str(branch_tail).expect("validated string input is non-empty")
    }

    async fn handle_event_action(
        &self,
        action: EventActionResponse,
        branch: &str,
        agent_type: AgentType,
    ) {
        match action {
            EventActionResponse::InjectMessage { message } => {
                let agent_name =
                    AgentName::try_from_str(branch).expect("validated string input is non-empty");
                let tab_name =
                    if let Ok(records) = self.ctx.agent_resolver().records_ref().try_read() {
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
                    &AgentName::try_from_str("event-handler")
                        .expect("literal validated string is non-empty"),
                    &message,
                    "Event handler action",
                )
                .await;
            }
            EventActionResponse::NotifyParent {
                message,
                pr_number: _pr_number,
            } => {
                let agent_slug = branch.rsplit_once('.').map(|(_, s)| s).unwrap_or(branch);
                let parent_session_id = branch
                    .rsplit_once('.')
                    .map(|(parent, _)| parent.to_string())
                    .unwrap_or_else(|| "root".to_string());
                let parent_name = AgentName::try_from_str(parent_session_id.as_str())
                    .expect("validated string input is non-empty");
                let parent_tab = crate::services::delivery::resolve_tab_name_for_agent(
                    &parent_name,
                    Some(self.ctx.agent_resolver()),
                );

                let agent_name = AgentName::try_from_str(agent_slug)
                    .expect("validated string input is non-empty");
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

    async fn deliver_release_message(&self, branch: &str, agent_type: AgentType, message: &str) {
        let agent_name =
            AgentName::try_from_str(branch).expect("validated string input is non-empty");
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
            &AgentName::try_from_str("event-handler")
                .expect("literal validated string is non-empty"),
            message,
            "Merge-ready release",
        )
        .await;
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

    async fn advance_reviewer_worktree_for_fixes(
        &self,
        registry: &PrRegistry,
        pr_number: u64,
        payload: &serde_json::Value,
    ) -> Result<()> {
        if payload.get("kind").and_then(|value| value.as_str()) != Some("fixes_pushed") {
            return Ok(());
        }

        let head_sha = payload
            .get("head_sha")
            .and_then(|value| value.as_str())
            .filter(|sha| !sha.is_empty())
            .ok_or_else(|| anyhow::anyhow!("fixes_pushed event missing head_sha"))?;
        let pr = registry
            .prs
            .get(&pr_number)
            .ok_or_else(|| anyhow::anyhow!("PR #{} not found in registry", pr_number))?;
        let reviewer_agent = pr
            .reviewer_agent
            .as_deref()
            .ok_or_else(|| anyhow::anyhow!("PR #{} has no reviewer agent", pr_number))?;
        let reviewer_dir = reviewer_worktree_path(self.ctx.project_dir(), reviewer_agent);

        let fetch = Command::new("git")
            .arg("-C")
            .arg(&reviewer_dir)
            .args(["fetch", "origin", pr.head_branch.as_str()])
            .output()
            .await;
        match fetch {
            Ok(output) if !output.status.success() => {
                warn!(
                    pr_number,
                    reviewer_agent,
                    path = %reviewer_dir.display(),
                    stderr = %String::from_utf8_lossy(&output.stderr).trim(),
                    "Reviewer worktree fetch failed before fixes_pushed checkout"
                );
            }
            Err(err) => {
                warn!(
                    pr_number,
                    reviewer_agent,
                    path = %reviewer_dir.display(),
                    error = %err,
                    "Failed to fetch reviewer worktree before fixes_pushed checkout"
                );
            }
            _ => {}
        }

        let output = Command::new("git")
            .arg("-C")
            .arg(&reviewer_dir)
            .args(["checkout", "--detach", head_sha])
            .output()
            .await
            .with_context(|| {
                format!(
                    "failed to checkout reviewer worktree {} at {}",
                    reviewer_dir.display(),
                    head_sha
                )
            })?;
        if !output.status.success() {
            anyhow::bail!(
                "failed to checkout reviewer worktree {} at {}: {}",
                reviewer_dir.display(),
                head_sha,
                String::from_utf8_lossy(&output.stderr).trim()
            );
        }

        Ok(())
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
    merge_blocked_on_ci: bool,
    branch: &str,
    format_message: &dyn Fn(&[LocalReviewComment], &[LocalReview]) -> String,
    max_rounds: u32,
) -> Vec<PendingAction> {
    let mut pending_actions = Vec::new();
    let comment_count = comments.len() + reviews.len();

    let now = Instant::now();
    let ci_changed = ci_status != old_state.last_ci_status;
    let ci_now_mergeable = ci_status == CIStatus::Success || ci_status == CIStatus::Neutral;
    if ci_changed {
        old_state.ci_mergeable_at = if ci_now_mergeable { Some(now) } else { None };
    }
    let merge_ready_now = !old_state.merge_ready_notified
        && signals_within_merge_ready_window(
            old_state.review_approved_at,
            old_state.ci_mergeable_at,
        );
    let recover_after_ci_block = merge_blocked_on_ci && ci_changed && ci_now_mergeable;

    if (old_state.notified_parent_approved || old_state.notified_parent_timeout || old_state.stuck)
        && !recover_after_ci_block
        && !merge_ready_now
    {
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
                    "head_sha": pr_sha,
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
        old_state.review_approved_at = Some(now);
        let merge_ready_now = !old_state.merge_ready_notified
            && signals_within_merge_ready_window(
                old_state.review_approved_at,
                old_state.ci_mergeable_at,
            );
        let kind = if merge_ready_now {
            "merge_ready"
        } else {
            "approved"
        };
        if merge_ready_now {
            old_state.merge_ready_notified = true;
        }
        pending_actions.push(PendingAction::WasmEvent {
            event_type: "pr_review",
            payload: serde_json::json!({
                "kind": kind,
                "pr_number": pr_number.as_u64(),
                "ci_status": ci_status.as_str(),
                "branch": branch,
            }),
        });
    }

    let changes_requested = reviews
        .iter()
        .any(|r| r.state == ReviewState::ChangesRequested);
    if changes_requested && old_state.last_review_state != ReviewState::ChangesRequested {
        old_state.last_review_state = ReviewState::ChangesRequested;
        old_state.rounds += 1;

        if old_state.rounds >= max_rounds {
            old_state.stuck = true;
            pending_actions.push(PendingAction::WasmEvent {
                event_type: "pr_review",
                payload: serde_json::json!({
                    "kind": "stuck",
                    "pr_number": pr_number.as_u64(),
                    "rounds": old_state.rounds,
                }),
            });
            pending_actions.push(PendingAction::WriteRegistryStuck {
                pr_number: pr_number.as_u64(),
                rounds: old_state.rounds,
            });
        } else {
            pending_actions.push(PendingAction::WasmEvent {
                event_type: "pr_review",
                payload: serde_json::json!({
                    "kind": "review_received",
                    "pr_number": pr_number.as_u64(),
                    "comments": format_message(comments, reviews),
                }),
            });
        }
    }

    if ci_changed {
        let reviewer_approved = old_state.notified_parent_approved;
        let ci_completed_merge_ready = !old_state.merge_ready_notified
            && signals_within_merge_ready_window(
                old_state.review_approved_at,
                old_state.ci_mergeable_at,
            );
        if ci_completed_merge_ready {
            old_state.merge_ready_notified = true;
        }
        pending_actions.push(PendingAction::WasmEvent {
            event_type: "ci_status",
            payload: serde_json::json!({
                "pr_number": pr_number.as_u64(),
                "status": ci_status.as_str(),
                "branch": branch,
                "merge_blocked_on_ci": merge_blocked_on_ci,
                "reviewer_approved": reviewer_approved,
                "merge_ready": ci_completed_merge_ready,
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
                "ci_status": ci_status.as_str(),
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

fn signals_within_merge_ready_window(
    review_approved_at: Option<Instant>,
    ci_mergeable_at: Option<Instant>,
) -> bool {
    let (Some(review_approved_at), Some(ci_mergeable_at)) = (review_approved_at, ci_mergeable_at)
    else {
        return false;
    };

    if review_approved_at >= ci_mergeable_at {
        review_approved_at.duration_since(ci_mergeable_at) <= MERGE_READY_SIGNAL_WINDOW
    } else {
        ci_mergeable_at.duration_since(review_approved_at) <= MERGE_READY_SIGNAL_WINDOW
    }
}

fn merge_ready_release_message(payload: &serde_json::Value) -> Option<String> {
    let kind_is_merge_ready = payload
        .get("kind")
        .and_then(|value| value.as_str())
        .is_some_and(|kind| kind == "merge_ready");
    let ci_event_is_merge_ready = payload
        .get("merge_ready")
        .and_then(|value| value.as_bool())
        .unwrap_or(false);

    if !kind_is_merge_ready && !ci_event_is_merge_ready {
        return None;
    }

    let pr_number = payload
        .get("pr_number")
        .and_then(|value| value.as_u64())
        .map(|value| value.to_string())
        .unwrap_or_else(|| "unknown".to_string());
    let status = payload
        .get("ci_status")
        .or_else(|| payload.get("status"))
        .and_then(|value| value.as_str())
        .unwrap_or("success");
    let branch = payload
        .get("branch")
        .and_then(|value| value.as_str())
        .unwrap_or("unknown");

    Some(format!(
        "[MERGE READY] PR #{} on {} has reviewer approval and CI {}. You may stop; the parent TL owns merge.",
        pr_number, branch, status
    ))
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

// ============================================================================
// Tangled CI event subscribers
// ============================================================================

/// Wire frame for events streamed by both the knot and the spindle.
#[derive(Deserialize)]
struct TangledStreamEvent {
    rkey: String,
    nsid: String,
    event: serde_json::Value,
}

/// Launch background tasks that subscribe to the knot and spindle WebSocket
/// streams and keep `ci_status_map` up to date.
async fn run_ci_subscriber(
    knot_url: Option<String>,
    spindle_url: Option<String>,
    ci_status_map: Arc<RwLock<HashMap<BranchName, CIStatus>>>,
    worktrees_dir: std::path::PathBuf,
    repo_name: Option<String>,
) {
    // pipeline rkey → branch name, populated from the knot's Pipeline events
    let pipeline_map: Arc<RwLock<HashMap<String, PipelineContext>>> =
        Arc::new(RwLock::new(HashMap::new()));
    let (ready_tx, ready_rx) = mpsc::channel(2);
    tokio::spawn(log_ci_pipeline_readiness(
        knot_url.clone(),
        spindle_url.clone(),
        worktrees_dir.clone(),
        ready_rx,
    ));

    let mut handles = Vec::new();

    if let Some(url) = knot_url {
        let pm = pipeline_map.clone();
        let wd = worktrees_dir.clone();
        let ready = ready_tx.clone();
        let repo = repo_name.clone();
        handles.push(tokio::spawn(async move {
            run_knot_subscriber(url, pm, wd, ready, repo).await;
        }));
    }

    if let Some(url) = spindle_url {
        let pm = pipeline_map.clone();
        let cs = ci_status_map.clone();
        let wd = worktrees_dir.clone();
        let ready = ready_tx.clone();
        handles.push(tokio::spawn(async move {
            run_spindle_subscriber(url, pm, cs, wd, ready).await;
        }));
    }
    drop(ready_tx);

    futures::future::join_all(handles).await;
}

async fn log_ci_pipeline_readiness(
    knot_url: Option<String>,
    spindle_url: Option<String>,
    worktrees_dir: std::path::PathBuf,
    mut ready_rx: mpsc::Receiver<CiSubscriberKind>,
) {
    let mut knot_ready = knot_url.is_none();
    let mut spindle_ready = spindle_url.is_none();
    let repo = worktrees_dir
        .parent()
        .and_then(Path::parent)
        .and_then(Path::file_name)
        .and_then(|name| name.to_str())
        .unwrap_or("unknown")
        .to_string();
    let deadline = tokio::time::sleep(Duration::from_secs(30));
    tokio::pin!(deadline);

    loop {
        if knot_ready && spindle_ready {
            info!(
                knot = knot_url.as_deref().unwrap_or("disabled"),
                spindle = spindle_url.as_deref().unwrap_or("disabled"),
                repo,
                "CI pipeline ready"
            );
            return;
        }

        tokio::select! {
            maybe_kind = ready_rx.recv() => {
                match maybe_kind {
                    Some(CiSubscriberKind::Knot) => knot_ready = true,
                    Some(CiSubscriberKind::Spindle) => spindle_ready = true,
                    None => return,
                }
            }
            _ = &mut deadline => {
                warn!(
                    knot = knot_url.as_deref().unwrap_or("disabled"),
                    spindle = spindle_url.as_deref().unwrap_or("disabled"),
                    knot_ready,
                    spindle_ready,
                    repo,
                    "CI pipeline degraded: subscriber connection not ready after 30s"
                );
                return;
            }
        }
    }
}

/// Subscribes to the knot's `/events` WebSocket and builds a mapping from
/// pipeline rkey → branch name by parsing `sh.tangled.pipeline` records.
async fn run_knot_subscriber(
    knot_url: String,
    pipeline_map: Arc<RwLock<HashMap<String, PipelineContext>>>,
    worktrees_dir: std::path::PathBuf,
    ready_tx: mpsc::Sender<CiSubscriberKind>,
    repo_name: Option<String>,
) {
    let mut reported_ready = false;
    loop {
        match tokio_tungstenite::connect_async(&knot_url).await {
            Ok((mut ws, _)) => {
                info!(url = %knot_url, "Knot CI subscriber connected");
                if !reported_ready {
                    let _ = ready_tx.send(CiSubscriberKind::Knot).await;
                    reported_ready = true;
                }
                while let Some(msg) = ws.next().await {
                    match msg {
                        Ok(tokio_tungstenite::tungstenite::Message::Text(text)) => {
                            if let Ok(ev) = serde_json::from_str::<TangledStreamEvent>(&text) {
                                if ev.nsid == "sh.tangled.pipeline" {
                                    if !pipeline_matches_repo(&ev.event, repo_name.as_deref()) {
                                        continue;
                                    }
                                    if let Some(branch_name) = extract_pipeline_branch(&ev.event) {
                                        let context =
                                            lookup_pipeline_context(&worktrees_dir, branch_name)
                                                .await;
                                        let worktree =
                                            worktrees_dir.join(context.branch_name.as_str());
                                        info!(
                                            rkey = %ev.rkey,
                                            branch = %context.branch_name,
                                            pr_number = context.pr_number,
                                            agent_name = context.agent_name.as_deref().unwrap_or("unknown"),
                                            worktree = %worktree.display(),
                                            "Spindle: CI initiated for worktree"
                                        );
                                        pipeline_map.write().await.insert(ev.rkey, context);
                                    }
                                }
                            }
                        }
                        Ok(tokio_tungstenite::tungstenite::Message::Close(_)) => break,
                        Err(e) => {
                            warn!(url = %knot_url, error = %e, "Knot subscriber error");
                            break;
                        }
                        _ => {}
                    }
                }
                warn!(url = %knot_url, "Knot CI subscriber disconnected, reconnecting in 15s");
            }
            Err(e) => {
                warn!(url = %knot_url, error = %e, "Knot CI subscriber failed to connect, retrying in 15s");
            }
        }
        tokio::time::sleep(Duration::from_secs(15)).await;
    }
}

fn pipeline_matches_repo(event: &serde_json::Value, expected_repo: Option<&str>) -> bool {
    let Some(expected_repo) = expected_repo else {
        return true;
    };
    event
        .get("triggerMetadata")
        .and_then(|tm| tm.get("repo"))
        .and_then(|repo| repo.get("repo"))
        .and_then(|repo| repo.as_str())
        .map(|repo| repo == expected_repo)
        .unwrap_or(false)
}

/// Subscribes to the spindle's `/events` WebSocket and updates `ci_status_map`
/// by parsing `sh.tangled.pipeline.status` events.
async fn run_spindle_subscriber(
    spindle_url: String,
    pipeline_map: Arc<RwLock<HashMap<String, PipelineContext>>>,
    ci_status_map: Arc<RwLock<HashMap<BranchName, CIStatus>>>,
    worktrees_dir: std::path::PathBuf,
    ready_tx: mpsc::Sender<CiSubscriberKind>,
) {
    let mut reported_ready = false;
    loop {
        match tokio_tungstenite::connect_async(&spindle_url).await {
            Ok((mut ws, _)) => {
                info!(url = %spindle_url, "Spindle CI subscriber connected");
                if !reported_ready {
                    let _ = ready_tx.send(CiSubscriberKind::Spindle).await;
                    reported_ready = true;
                }
                while let Some(msg) = ws.next().await {
                    match msg {
                        Ok(tokio_tungstenite::tungstenite::Message::Text(text)) => {
                            if let Ok(ev) = serde_json::from_str::<TangledStreamEvent>(&text) {
                                if ev.nsid == "sh.tangled.pipeline.status" {
                                    if let Some((rkey, status)) = extract_pipeline_status(&ev.event)
                                    {
                                        let context = pipeline_map.read().await.get(&rkey).cloned();
                                        if let Some(context) = context {
                                            let ci = CIStatus::parse(&status);
                                            let worktree =
                                                worktrees_dir.join(context.branch_name.as_str());
                                            info!(
                                                rkey = %rkey,
                                                branch = %context.branch_name,
                                                pr_number = context.pr_number,
                                                agent_name = context.agent_name.as_deref().unwrap_or("unknown"),
                                                status = %status,
                                                worktree = %worktree.display(),
                                                "Spindle: CI status updated"
                                            );
                                            ci_status_map
                                                .write()
                                                .await
                                                .insert(context.branch_name, ci);
                                        } else {
                                            info!(rkey = %rkey, status = %status, "Spindle: CI event received but no branch mapping for rkey yet (pipeline may not have registered)");
                                        }
                                    }
                                }
                            }
                        }
                        Ok(tokio_tungstenite::tungstenite::Message::Close(_)) => break,
                        Err(e) => {
                            warn!(url = %spindle_url, error = %e, "Spindle subscriber error");
                            break;
                        }
                        _ => {}
                    }
                }
                warn!(url = %spindle_url, "Spindle CI subscriber disconnected, reconnecting in 15s");
            }
            Err(e) => {
                warn!(url = %spindle_url, error = %e, "Spindle CI subscriber failed to connect, retrying in 15s");
            }
        }
        tokio::time::sleep(Duration::from_secs(15)).await;
    }
}

async fn lookup_pipeline_context(worktrees_dir: &Path, branch_name: BranchName) -> PipelineContext {
    let Some(prs_path) = worktrees_dir
        .parent()
        .map(|exo_dir| exo_dir.join("prs.json"))
    else {
        return PipelineContext {
            branch_name,
            pr_number: None,
            agent_name: None,
        };
    };

    match read_pr_registry(&prs_path).await {
        Ok(registry) => registry
            .find_by_branch(&branch_name)
            .map(|pr| PipelineContext {
                branch_name: branch_name.clone(),
                pr_number: Some(pr.number),
                agent_name: Some(pr.author_agent.clone()),
            })
            .unwrap_or(PipelineContext {
                branch_name,
                pr_number: None,
                agent_name: None,
            }),
        Err(error) => {
            debug!(
                path = %prs_path.display(),
                branch = %branch_name,
                error = %error,
                "Unable to enrich CI pipeline event from local PR registry"
            );
            PipelineContext {
                branch_name,
                pr_number: None,
                agent_name: None,
            }
        }
    }
}

fn normalize_tangled_event_ws_url(input: &str) -> String {
    let trimmed = input.trim();
    match Url::parse(trimmed) {
        Ok(mut url) => {
            match url.scheme() {
                "http" => {
                    let _ = url.set_scheme("ws");
                }
                "https" => {
                    let _ = url.set_scheme("wss");
                }
                _ => {}
            }
            let path = url.path().trim_end_matches('/');
            if path.is_empty() {
                url.set_path("/events");
            } else if !path.ends_with("/events") {
                url.set_path(&format!("{path}/events"));
            }
            url.to_string()
        }
        Err(_) => {
            let trimmed = trimmed.trim_end_matches('/');
            if trimmed.ends_with("/events") {
                trimmed.to_string()
            } else {
                format!("{trimmed}/events")
            }
        }
    }
}

/// Extracts the branch name from a `sh.tangled.pipeline` event payload.
/// Returns `None` if the trigger is not a push or has no ref field.
fn extract_pipeline_branch(event: &serde_json::Value) -> Option<BranchName> {
    let ref_str = event
        .get("triggerMetadata")
        .and_then(|tm| tm.get("push"))
        .and_then(|push| push.get("ref"))
        .and_then(|r| r.as_str())?;

    let branch = ref_str.strip_prefix("refs/heads/").unwrap_or(ref_str);
    if branch.is_empty() {
        None
    } else {
        Some(BranchName::try_from_str(branch).expect("validated string input is non-empty"))
    }
}

/// Extracts the pipeline rkey and status string from a `sh.tangled.pipeline.status` event payload.
/// The pipeline field is an AT-URI; the rkey is the last path segment.
fn extract_pipeline_status(event: &serde_json::Value) -> Option<(String, String)> {
    let pipeline_uri = event.get("pipeline").and_then(|p| p.as_str())?;
    let status = event.get("status").and_then(|s| s.as_str())?;
    let rkey = pipeline_uri.rsplit('/').next()?.to_string();
    if rkey.is_empty() {
        None
    } else {
        Some((rkey, status.to_string()))
    }
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

    fn test_state(branch: &BranchName, agent_type: AgentType, sha: &str) -> WatchState {
        WatchState::new(branch, agent_type, sha, CIStatus::Unknown, 0)
    }

    fn test_comment(body: &str) -> LocalReviewComment {
        LocalReviewComment {
            body: body.to_string(),
            path: None,
            diff_hunk: None,
            thread_id: None,
            resolved: false,
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
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "def456",
            &[],
            &[],
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );
        assert!(actions
            .iter()
            .any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "commits_pushed")));
        assert_eq!(state.last_sha, "def456");
    }

    #[test]
    fn test_sha_change_after_changes_requested_fires_fixes_pushed() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
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
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );
        let fixes_payload = actions
            .iter()
            .find_map(|a| match a {
                PendingAction::WasmEvent { payload, .. } if payload["kind"] == "fixes_pushed" => {
                    Some(payload)
                }
                _ => None,
            })
            .expect("fixes_pushed event should be emitted");
        assert_eq!(fixes_payload["head_sha"], "def456");
        assert!(state.addressed_changes);
        assert_eq!(state.last_review_state, ReviewState::None);
    }

    fn pr_with_reviewer(
        pr_number: u64,
        agent_name: &str,
        birth_branch: &str,
    ) -> crate::services::file_pr_local::PrEntry {
        let mut pr = test_pr_entry();
        pr.number = pr_number;
        pr.reviewer_agent = Some(agent_name.to_string());
        pr.reviewer_birth_branch = Some(birth_branch.to_string());
        pr
    }

    #[test]
    fn test_reviewer_fanout_decision_dispatches_when_reviewer_registered() {
        let registry = test_registry(pr_with_reviewer(7, "review-pr-7-codex", "review-pr-7"));
        let payload = serde_json::json!({ "kind": "fixes_pushed", "pr_number": 7 });

        let decision = reviewer_fanout_decision("pr_review", &payload, 7, &registry);
        match decision {
            ReviewerFanOut::DispatchTo(branch, agent_type, role) => {
                assert_eq!(branch.as_str(), "review-pr-7");
                assert_eq!(agent_type, AgentType::Codex);
                assert_eq!(role, "reviewer");
            }
            other => panic!("expected DispatchTo, got {other:?}"),
        }
    }

    #[test]
    fn test_reviewer_fanout_decision_reports_missing_reviewer() {
        let registry = test_registry(test_pr_entry()); // reviewer fields are None
        let payload = serde_json::json!({ "kind": "fixes_pushed", "pr_number": 1 });

        assert_eq!(
            reviewer_fanout_decision("pr_review", &payload, 1, &registry),
            ReviewerFanOut::NoReviewer,
            "fixes_pushed with no registered reviewer must surface NoReviewer so the \
             caller logs the stall condition"
        );
    }

    #[test]
    fn test_reviewer_fanout_decision_skips_non_pr_review_events() {
        let registry = test_registry(pr_with_reviewer(1, "review-pr-1-codex", "review-pr-1"));
        let payload = serde_json::json!({ "kind": "fixes_pushed", "pr_number": 1 });

        assert_eq!(
            reviewer_fanout_decision("ci_status", &payload, 1, &registry),
            ReviewerFanOut::NotApplicable,
            "non pr_review event types must never fan out, even if the kind happens \
             to match"
        );
    }

    #[test]
    fn test_reviewer_fanout_decision_dispatches_for_every_pr_review_kind() {
        // Per #250: every pr_review kind currently emitted by the watcher has a
        // corresponding handler in ReviewerRole.hs, so all of them fan out. The
        // Haskell handler decides what to do per kind (some kinds the reviewer
        // caused itself; the handler may return NoAction).
        let registry = test_registry(pr_with_reviewer(1, "review-pr-1-codex", "review-pr-1"));
        for kind in [
            "fixes_pushed",
            "commits_pushed",
            "review_received",
            "approved",
            "timeout",
            "reviewer_approved",
            "reviewer_requested_changes",
            "rate_limited",
            "stuck",
            "merge_ready",
        ] {
            let payload = serde_json::json!({ "kind": kind, "pr_number": 1 });
            match reviewer_fanout_decision("pr_review", &payload, 1, &registry) {
                ReviewerFanOut::DispatchTo(branch, agent_type, role) => {
                    assert_eq!(branch.as_str(), "review-pr-1");
                    assert_eq!(agent_type, AgentType::Codex, "kind {kind}");
                    assert_eq!(role, "reviewer", "kind {kind}");
                }
                other => panic!("kind {kind} expected DispatchTo, got {other:?}"),
            }
        }
    }

    #[test]
    fn test_reviewer_fanout_uses_reviewer_role_not_runtime_role() {
        let registry = test_registry(pr_with_reviewer(1, "review-pr-1-codex", "review-pr-1"));
        let payload = serde_json::json!({ "kind": "fixes_pushed", "pr_number": 1 });

        match reviewer_fanout_decision("pr_review", &payload, 1, &registry) {
            ReviewerFanOut::DispatchTo(_, agent_type, role) => {
                assert_eq!(agent_type, AgentType::Codex);
                assert_eq!(
                    legacy_event_role_for_agent_type(agent_type),
                    "dev",
                    "runtime-derived role would route this reviewer through DevRole"
                );
                assert_eq!(
                    role, "reviewer",
                    "reviewer fan-out must select ReviewerRole handlers explicitly"
                );
            }
            other => panic!("expected DispatchTo, got {other:?}"),
        }
    }

    #[test]
    fn test_new_comments_fire_review_received() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let comments = vec![test_comment("Fix this")];
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &comments,
            &[],
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, _| "review message".to_string(),
            5,
        );
        assert!(actions
            .iter()
            .any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "review_received")));
        assert_eq!(state.last_comment_count, 1);
    }

    #[test]
    fn test_approval_fires_approved() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review("LGTM!", ReviewState::Approved)];
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &reviews,
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );
        assert!(actions
            .iter()
            .any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "approved")));
        assert!(state.notified_parent_approved);
    }

    #[test]
    fn test_approval_after_green_ci_fires_merge_ready() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.last_ci_status = CIStatus::Success;
        state.ci_mergeable_at = Some(Instant::now() - Duration::from_secs(60));
        let reviews = vec![test_review("LGTM!", ReviewState::Approved)];

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &reviews,
            CIStatus::Success,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );

        assert!(actions.iter().any(|a| matches!(
            a,
            PendingAction::WasmEvent {
                event_type: "pr_review",
                payload,
            } if payload["kind"] == "merge_ready" && payload["ci_status"] == "success"
        )));
        assert!(state.merge_ready_notified);
    }

    #[test]
    fn test_initial_approved_green_ci_observation_fires_merge_ready() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review("LGTM!", ReviewState::Approved)];

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &reviews,
            CIStatus::Success,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );

        assert!(actions.iter().any(|a| matches!(
            a,
            PendingAction::WasmEvent {
                event_type: "pr_review",
                payload,
            } if payload["kind"] == "merge_ready"
        )));
        assert!(actions.iter().any(|a| matches!(
            a,
            PendingAction::WasmEvent {
                event_type: "ci_status",
                payload,
            } if payload["status"] == "success"
        )));
        assert!(state.merge_ready_notified);
    }

    #[test]
    fn test_green_ci_after_approval_fires_merge_ready_ci_event() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.notified_parent_approved = true;
        state.last_review_state = ReviewState::Approved;
        state.review_approved_at = Some(Instant::now() - Duration::from_secs(60));
        state.last_ci_status = CIStatus::Pending;

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Success,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );

        assert!(actions.iter().any(|a| matches!(
            a,
            PendingAction::WasmEvent {
                event_type: "ci_status",
                payload,
            } if payload["merge_ready"] == true && payload["reviewer_approved"] == true
        )));
        assert!(state.merge_ready_notified);
    }

    #[test]
    fn test_merge_ready_review_payload_builds_dev_release_message() {
        let payload = serde_json::json!({
            "kind": "merge_ready",
            "pr_number": 7,
            "ci_status": "success",
            "branch": "main.feature.dev",
        });

        let message = merge_ready_release_message(&payload).unwrap();

        assert!(message.contains("[MERGE READY] PR #7"));
        assert!(message.contains("main.feature.dev"));
        assert!(message.contains("You may stop"));
    }

    #[test]
    fn test_merge_ready_ci_payload_builds_dev_release_message() {
        let payload = serde_json::json!({
            "pr_number": 8,
            "status": "neutral",
            "branch": "main.feature.dev",
            "merge_ready": true,
        });

        let message = merge_ready_release_message(&payload).unwrap();

        assert!(message.contains("[MERGE READY] PR #8"));
        assert!(message.contains("CI neutral"));
    }

    #[test]
    fn test_non_merge_ready_payload_has_no_release_message() {
        let payload = serde_json::json!({
            "kind": "approved",
            "pr_number": 9,
        });

        assert!(merge_ready_release_message(&payload).is_none());
    }

    #[test]
    fn test_tangled_event_url_normalizes_base_websocket_url() {
        assert_eq!(
            normalize_tangled_event_ws_url("ws://localhost:5555"),
            "ws://localhost:5555/events"
        );
        assert_eq!(
            normalize_tangled_event_ws_url("ws://localhost:6555/"),
            "ws://localhost:6555/events"
        );
    }

    #[test]
    fn test_tangled_event_url_preserves_explicit_events_path() {
        assert_eq!(
            normalize_tangled_event_ws_url("ws://localhost:5555/events"),
            "ws://localhost:5555/events"
        );
        assert_eq!(
            normalize_tangled_event_ws_url("ws://localhost:5555/events?cursor=12"),
            "ws://localhost:5555/events?cursor=12"
        );
    }

    #[test]
    fn test_tangled_event_url_converts_http_scheme_for_websocket_subscription() {
        assert_eq!(
            normalize_tangled_event_ws_url("http://localhost:5555"),
            "ws://localhost:5555/events"
        );
        assert_eq!(
            normalize_tangled_event_ws_url("https://example.test"),
            "wss://example.test/events"
        );
    }

    #[test]
    fn test_pipeline_repo_filter_matches_current_project() {
        let event = serde_json::json!({
            "triggerMetadata": {
                "repo": {
                    "did": "did:plc:localdev",
                    "knot": "localhost:5555",
                    "repo": "backrooms-workspace"
                }
            }
        });

        assert!(pipeline_matches_repo(&event, Some("backrooms-workspace")));
        assert!(!pipeline_matches_repo(&event, Some("ci-test")));
        assert!(pipeline_matches_repo(&event, None));
    }

    #[test]
    fn test_pipeline_repo_filter_rejects_missing_repo() {
        let event = serde_json::json!({
            "triggerMetadata": {
                "push": {"ref": "refs/heads/main"}
            }
        });

        assert!(!pipeline_matches_repo(&event, Some("backrooms-workspace")));
    }

    #[test]
    fn test_green_ci_after_stale_approval_does_not_fire_merge_ready() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.notified_parent_approved = true;
        state.last_review_state = ReviewState::Approved;
        state.review_approved_at =
            Some(Instant::now() - MERGE_READY_SIGNAL_WINDOW - Duration::from_secs(1));
        state.last_ci_status = CIStatus::Pending;

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Success,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );

        assert!(actions.is_empty());
        assert!(!state.merge_ready_notified);
    }

    #[test]
    fn test_changes_requested_fires_review_received() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review("Needs work", ReviewState::ChangesRequested)];
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &reviews,
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );
        assert_eq!(state.last_review_state, ReviewState::ChangesRequested);
        assert!(actions.iter().any(|a| matches!(
            a,
            PendingAction::WasmEvent {
                event_type: "pr_review",
                ..
            }
        )));
    }

    #[test]
    fn test_changes_requested_at_max_rounds_fires_stuck() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review(
            "Still needs work",
            ReviewState::ChangesRequested,
        )];
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &reviews,
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, _| "Still needs work".to_string(),
            1,
        );

        assert!(state.stuck);
        assert_eq!(state.rounds, 1);
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent {
                event_type: "pr_review",
                payload,
            } if payload["kind"] == "stuck" && payload["rounds"] == 1
        )));
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WriteRegistryStuck {
                pr_number: 1,
                rounds: 1,
            }
        )));
    }

    #[test]
    fn test_ci_change_fires_event() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.last_ci_status = CIStatus::Pending;
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Success,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );
        assert!(actions.iter().any(|a| matches!(
            a,
            PendingAction::WasmEvent {
                event_type: "ci_status",
                ..
            }
        )));
        assert_eq!(state.last_ci_status, CIStatus::Success);
    }

    #[test]
    fn test_timeout_after_15_minutes() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.first_seen = Instant::now() - Duration::from_secs(16 * 60);
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );
        assert!(actions
            .iter()
            .any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "timeout")));
        assert!(state.notified_parent_timeout);
    }

    #[test]
    fn test_stale_guard_suppresses_after_approval() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.notified_parent_approved = true;
        state.last_ci_status = CIStatus::Pending;
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "def456",
            &[test_comment("late")],
            &[test_review("late review", ReviewState::Approved)],
            CIStatus::Success,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );
        assert!(actions.is_empty());
    }

    #[test]
    fn test_ci_success_after_merge_block_bypasses_stale_guard() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.notified_parent_approved = true;
        state.last_ci_status = CIStatus::Pending;

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Success,
            true,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );

        assert!(actions.iter().any(|a| matches!(
            a,
            PendingAction::WasmEvent {
                event_type: "ci_status",
                payload,
            } if payload["merge_blocked_on_ci"] == true && payload["status"] == "success"
        )));
        assert_eq!(state.last_ci_status, CIStatus::Success);
    }

    #[test]
    fn test_no_duplicate_approval() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
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
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );
        assert!(actions.is_empty());
    }

    #[test]
    fn test_approval_detected_from_body_text() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
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
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );
        assert!(actions
            .iter()
            .any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "approved")));
    }

    #[test]
    fn test_timeout_shorter_after_addressed_changes() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
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
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );
        assert!(actions
            .iter()
            .any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "timeout" && payload["minutes_elapsed"] == 5)));
    }

    #[test]
    fn test_no_ci_event_when_status_unchanged() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.last_ci_status = CIStatus::Success;
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Success,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
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
        assert!(reviews
            .iter()
            .any(|r| r.state == ReviewState::ChangesRequested));
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
            LocalReview {
                body: "LGTM!".to_string(),
                state: ReviewState::Approved,
            },
            LocalReview {
                body: "Good work.".to_string(),
                state: ReviewState::None,
            },
        ];
        let msg = format_review_message(&[], &reviews);
        assert!(msg.contains("Review summary:"));
        assert!(msg.contains("LGTM!"));
        assert!(msg.contains("Good work."));
    }

    #[test]
    fn test_format_message_with_inline_comments() {
        let comments = vec![LocalReviewComment {
            body: "Fix this typo".to_string(),
            path: Some("src/main.rs".to_string()),
            diff_hunk: Some("@@ -1,3 +1,3 @@".to_string()),
            thread_id: None,
            resolved: false,
        }];
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
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
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
                {"body": "Needs more tests", "path": "src/lib.rs"}
            ]
        }"#;
        let rf: ReviewFile = serde_json::from_str(json).unwrap();
        assert_eq!(rf.state, "changes_requested");
        assert_eq!(rf.comments.len(), 1);
        assert_eq!(rf.comments[0].body, "Needs more tests");
    }

    // ---------------------------------------------------------------------------
    // Reviewer spawner tests
    // ---------------------------------------------------------------------------

    struct MockReviewerSpawner {
        called: Arc<std::sync::atomic::AtomicBool>,
    }

    #[async_trait::async_trait]
    impl ReviewerSpawner for MockReviewerSpawner {
        async fn spawn_reviewer_for_pr(
            &self,
            _pr: &crate::services::file_pr_local::PrEntry,
        ) -> anyhow::Result<()> {
            self.called.store(true, std::sync::atomic::Ordering::SeqCst);
            Ok(())
        }
    }

    fn test_pr_entry() -> crate::services::file_pr_local::PrEntry {
        crate::services::file_pr_local::PrEntry {
            number: 1,
            head_branch: "main.feat-gemini".to_string(),
            base_branch: "main".to_string(),
            title: "Test PR".to_string(),
            body: String::new(),
            author_agent: "feat-gemini".to_string(),
            author_role: "dev".to_string(),
            created_at: chrono::Utc::now(),
            state: crate::services::file_pr_local::PrState::Open,
            review_state: crate::services::file_pr_local::LocalReviewState::PendingReview,
            last_review_at: None,
            last_head_sha: None,
            reviewer_agent: None,
            reviewer_birth_branch: None,
            rounds: 0,
            stuck: false,
            needs_human_review: false,
            merge_blocked_on_ci: false,
        }
    }

    fn test_registry(
        pr: crate::services::file_pr_local::PrEntry,
    ) -> crate::services::file_pr_local::PrRegistry {
        let mut prs = HashMap::new();
        prs.insert(pr.number, pr);
        crate::services::file_pr_local::PrRegistry {
            prs,
            next_number: 2,
        }
    }

    fn test_observation(sha: &str) -> Observation {
        Observation {
            head_sha: sha.to_string(),
            review_state: crate::services::file_pr_local::LocalReviewState::PendingReview,
            comments: vec![],
            ci_status: CIStatus::Unknown,
        }
    }

    #[tokio::test]
    async fn test_resolve_event_agent_name_uses_reviewer_birth_branch() {
        let temp_dir = tempfile::tempdir().unwrap();
        let resolver = crate::services::AgentResolver::load(temp_dir.path().to_path_buf()).await;
        resolver
            .register(crate::services::AgentIdentityRecord {
                agent_name: AgentName::try_from_str("review-pr-1-codex")
                    .expect("literal validated string is non-empty"),
                slug: crate::domain::Slug::try_from_str("review-pr-1")
                    .expect("literal validated string is non-empty"),
                agent_type: AgentType::Codex,
                birth_branch: BirthBranch::try_from_str("review-pr-1")
                    .expect("literal validated string is non-empty"),
                parent_branch: BirthBranch::try_from_str("main")
                    .expect("literal validated string is non-empty"),
                working_dir: std::path::PathBuf::from(".exo/worktrees/review-pr-1-codex"),
                display_name: "review-pr-1-codex".to_string(),
                topology: crate::services::agent_control::Topology::WorktreePerAgent,
            })
            .await
            .unwrap();

        let mut services = crate::services::Services::test();
        services.agent_resolver = Arc::new(resolver);
        let watcher = WorktreeEventWatcher::new(Arc::new(services));

        let agent_name = watcher
            .resolve_event_agent_name("review-pr-1", AgentType::Codex)
            .await;

        assert_eq!(agent_name.as_str(), "review-pr-1-codex");
    }

    #[tokio::test]
    async fn test_process_observations_persists_last_head_sha() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services));
        let pr = test_pr_entry();
        let registry = test_registry(pr);
        write_pr_registry(&watcher.prs_path, &registry)
            .await
            .unwrap();

        let mut observations = HashMap::new();
        observations.insert(1u64, test_observation("def456"));

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();

        let persisted = read_pr_registry(&watcher.prs_path).await.unwrap();
        assert_eq!(
            persisted
                .prs
                .get(&1)
                .and_then(|pr| pr.last_head_sha.as_deref()),
            Some("def456")
        );
    }

    #[tokio::test]
    async fn test_reviewer_spawner_called_for_new_pr() {
        use std::sync::atomic::{AtomicBool, Ordering};

        let called = Arc::new(AtomicBool::new(false));
        let spawner = Arc::new(MockReviewerSpawner {
            called: called.clone(),
        });

        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_reviewer_spawner(spawner);

        let pr = test_pr_entry();
        let registry = test_registry(pr);
        let mut observations = HashMap::new();
        observations.insert(1u64, test_observation("abc123"));

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();

        // Give the tokio::spawn task a moment to complete.
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        assert!(
            called.load(Ordering::SeqCst),
            "spawner should be called for first sighting of new PR"
        );

        let state = watcher.state.lock().await;
        assert!(
            state.get(&1).map(|s| s.reviewer_spawned).unwrap_or(false),
            "reviewer_spawned should be true after first sighting"
        );
    }

    #[tokio::test]
    async fn test_reviewer_spawner_not_called_twice() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        let call_count = Arc::new(AtomicUsize::new(0));

        struct CountingSpawner {
            count: Arc<AtomicUsize>,
        }
        #[async_trait::async_trait]
        impl ReviewerSpawner for CountingSpawner {
            async fn spawn_reviewer_for_pr(
                &self,
                _pr: &crate::services::file_pr_local::PrEntry,
            ) -> anyhow::Result<()> {
                self.count.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }
        }

        let spawner = Arc::new(CountingSpawner {
            count: call_count.clone(),
        });

        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_reviewer_spawner(spawner);

        let pr = test_pr_entry();
        let registry = test_registry(pr);
        let mut observations = HashMap::new();
        observations.insert(1u64, test_observation("abc123"));

        // First call — spawns reviewer
        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        // Second call — PR is already in state, no second spawn
        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        assert_eq!(
            call_count.load(Ordering::SeqCst),
            1,
            "spawner should only be called once per PR"
        );
    }
}
