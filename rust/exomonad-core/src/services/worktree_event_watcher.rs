use crate::domain::{AgentName, BirthBranch, BranchName, CIStatus, PRNumber};
use crate::plugin_manager::PluginManager;
use crate::services::agent_control::AgentType;
use crate::services::agent_resources::dispose_reviewers_for_pr;
use crate::services::file_pr_local::{
    read_pr_registry, write_pr_registry, LocalReviewState, PrEntry, PrRegistry, PrState,
};
use crate::services::review_policy::ReviewPolicy;
use crate::services::{
    CiStatusKey, CiStatusMap, HasAcpRegistry, HasAgentResolver, HasEventLog, HasEventQueue,
    HasGitWorktreeService, HasProjectDir, HasTeamRegistry, ReviewerSpawner,
};
use anyhow::{Context, Result};
use chrono::Utc;
use exomonad_proto::effects::events::{event::EventType, AgentMessage, Event};
use exomonad_proto::effects::file_pr::LocalPrResponse;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};
use tokio::process::Command;
use tokio::sync::{Mutex, RwLock};
use tracing::{debug, info, instrument, warn};

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
    author_branch: Option<String>,
}

/// A local review with typed state.
#[derive(Debug, Clone, Serialize)]
struct LocalReview {
    body: String,
    state: ReviewState,
    author_branch: Option<String>,
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
    WriteRegistryRounds {
        pr_number: u64,
        rounds: u32,
    },
    FileHumanEscalation {
        pr_number: u64,
        classification: ReviewStallKind,
        diagnostic: ReviewStallDiagnostic,
    },
    TriggerManualCi {
        pr_number: u64,
        branch: String,
        head_sha: String,
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
    /// Event was authored by the registered reviewer and should not be echoed back.
    SuppressedSelfEcho,
}

fn reviewer_worktree_path(project_dir: &Path, reviewer_agent: &str) -> PathBuf {
    project_dir.join(".exo/worktrees").join(reviewer_agent)
}

/// Decide whether to fan a PR review event out to the reviewer.
///
/// Every `pr_review` event kind currently emitted by the watcher has a corresponding
/// handler in `.exo/roles/devswarm/ReviewerRole.hs` (`fixes_pushed`, `commits_pushed`,
/// `approved`, `reviewer_approved`, `reviewer_requested_changes`, `rate_limited`,
/// `merge_ready`), so most fan-out is keyed by event_type alone. `review_received`
/// is the exception: when the registered reviewer authored the review file, the
/// leaf needs the feedback and the reviewer does not need its own comment echoed back.
///
/// Non-`pr_review` event_types (`ci_status`, `agent.sibling_merged`, etc.) remain
/// leaf-only because the reviewer has no handler for them.
fn reviewer_fanout_decision(
    event_type: &str,
    payload: &serde_json::Value,
    pr_number: u64,
    registry: &PrRegistry,
) -> ReviewerFanOut {
    if event_type != "pr_review" {
        return ReviewerFanOut::NotApplicable;
    }
    if payload
        .get("kind")
        .and_then(|value| value.as_str())
        .is_some_and(|kind| matches!(kind, "ci_triggered" | "ci_blocked"))
    {
        return ReviewerFanOut::NotApplicable;
    }
    let Some((branch, agent_type)) = registry.reviewer_for_pr(pr_number) else {
        return ReviewerFanOut::NoReviewer;
    };

    if payload.get("kind").and_then(|value| value.as_str()) == Some("review_received")
        && payload
            .get("author_branch")
            .and_then(|value| value.as_str())
            .is_some_and(|author_branch| author_branch == branch.as_str())
    {
        ReviewerFanOut::SuppressedSelfEcho
    } else {
        ReviewerFanOut::DispatchTo(branch, agent_type, "reviewer")
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
    pr_review_cycle_count: usize,
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
    ci_triggered_sha: Option<String>,
    ci_blocked_notified: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum ReviewStallKind {
    DevNotPushing,
    ReviewerNotResponding,
    ReviewerNeverStarted,
    DevFailed,
    CiFailed,
}

const REVIEW_STALL_KINDS: [ReviewStallKind; 5] = [
    ReviewStallKind::DevNotPushing,
    ReviewStallKind::ReviewerNotResponding,
    ReviewStallKind::ReviewerNeverStarted,
    ReviewStallKind::DevFailed,
    ReviewStallKind::CiFailed,
];

impl ReviewStallKind {
    fn as_str(self) -> &'static str {
        match self {
            ReviewStallKind::DevNotPushing => "dev_not_pushing",
            ReviewStallKind::ReviewerNotResponding => "reviewer_not_responding",
            ReviewStallKind::ReviewerNeverStarted => "reviewer_never_started",
            ReviewStallKind::DevFailed => "dev_failed",
            ReviewStallKind::CiFailed => "ci_failed",
        }
    }

    fn title_fragment(self) -> &'static str {
        match self {
            ReviewStallKind::DevNotPushing => "dev leaf stopped pushing fixes",
            ReviewStallKind::ReviewerNotResponding => "reviewer stopped responding",
            ReviewStallKind::ReviewerNeverStarted => "reviewer never started",
            ReviewStallKind::DevFailed => "dev leaf reported failure",
            ReviewStallKind::CiFailed => "CI failed after reviewer approval",
        }
    }
}

#[derive(Debug, Clone, Serialize)]
struct ReviewStallDiagnostic {
    branch: String,
    head_sha: String,
    last_observed_sha: String,
    rounds: u32,
    reviewer_registered: bool,
    review_file_seen: bool,
    review_file_mtime: Option<String>,
    wait_seconds: u64,
    ci_status: String,
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
            pr_review_cycle_count: comment_count,
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
            ci_triggered_sha: None,
            ci_blocked_notified: false,
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
    #[serde(default)]
    verdicts: Vec<ReviewVerdictEntry>,
}

#[derive(Debug, Clone, Deserialize)]
struct ReviewVerdictEntry {
    #[serde(default)]
    state: String,
    #[serde(default)]
    body: String,
    #[serde(default)]
    comments: Vec<ReviewCommentEntry>,
    #[serde(default)]
    author_branch: Option<String>,
    #[serde(default)]
    head_sha: Option<String>,
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
    #[serde(default)]
    author_branch: Option<String>,
}

/// Observation collected from local sources for one open PR.
struct Observation {
    head_sha: String,
    review_state: LocalReviewState,
    comments: Vec<LocalReviewComment>,
    reviews: Vec<LocalReview>,
    ci_status: CIStatus,
    review_file_seen: bool,
    review_file_mtime: Option<SystemTime>,
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
    ci_status_map: Arc<RwLock<CiStatusMap>>,
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
        + HasGitWorktreeService
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

    pub fn with_reviewer_spawner(mut self, spawner: Arc<dyn ReviewerSpawner>) -> Self {
        self.reviewer_spawner = Some(spawner);
        self
    }

    /// Use a shared CI status map (e.g. from `Services`) instead of the internal one.
    ///
    /// Call this so the merge handler and the watcher read from the same map.
    pub fn with_ci_status_map(mut self, map: Arc<RwLock<CiStatusMap>>) -> Self {
        self.ci_status_map = map;
        self
    }

    fn ci_source_configured(&self) -> bool {
        true
    }

    async fn observed_ci_status(&self, branch: &BranchName, head_sha: &str) -> CIStatus {
        if !self.policy.ci.gate.enabled(self.ci_source_configured()) {
            return CIStatus::Neutral;
        }

        self.ci_status_map
            .read()
            .await
            .get(&ci_status_key(branch, head_sha))
            .copied()
            .unwrap_or(CIStatus::Unknown)
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

            let sleep_duration = if consecutive_failures == 0 {
                base_interval
            } else {
                let backoff = base_interval * 2u32.saturating_pow(consecutive_failures.min(6));
                backoff.min(max_backoff)
            };

            tokio::time::sleep(sleep_duration).await;
        }
    }

    async fn bootstrap_registry_from_reviews(&self) -> Result<PrRegistry> {
        Ok(PrRegistry::default())
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

    async fn set_pr_rounds(&self, pr_number: u64, rounds: u32) -> anyhow::Result<()> {
        let mut registry: PrRegistry = read_pr_registry(&self.prs_path).await?;
        if let Some(pr) = registry.prs.get_mut(&pr_number) {
            if pr.rounds != rounds {
                pr.rounds = rounds;
                write_pr_registry(&self.prs_path, &registry).await?;
                info!(pr_number, rounds, "Persisted PR review rounds");
            }
        }
        Ok(())
    }

    #[instrument(skip_all, name = "worktree_event_watcher.poll_cycle")]
    async fn poll_cycle(&self) -> Result<()> {
        let mut registry = match read_pr_registry(&self.prs_path).await {
            Ok(r) => r,
            Err(_) => return Ok(()),
        };

        if registry.prs.is_empty() {
            registry = self.bootstrap_registry_from_reviews().await?;
            if registry.prs.is_empty() {
                return Ok(());
            }
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

            let review_path = project_dir
                .join(".exo/reviews")
                .join(format!("pr_{}.json", number));
            let review_file_mtime = tokio::fs::metadata(&review_path)
                .await
                .ok()
                .and_then(|metadata| metadata.modified().ok());
            let review_file = Self::read_review_file(&project_dir, *number).await;
            let review_file_seen = review_file.is_some();
            let (review_state, comments, reviews) = match review_file {
                Some(rf) => review_file_parts(rf, Some(&head_sha)),
                None => {
                    let state = pr.review_state.clone();
                    (state, vec![], vec![])
                }
            };

            let branch = BranchName::try_from_str(pr.head_branch.as_str())
                .expect("validated string input is non-empty");
            let ci_status = self.observed_ci_status(&branch, &head_sha).await;

            observations.insert(
                *number,
                Observation {
                    head_sha,
                    review_state,
                    comments,
                    reviews,
                    ci_status,
                    review_file_seen,
                    review_file_mtime,
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
        let mut review_state_updates: Vec<(u64, LocalReviewState, String)> = Vec::new();

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
                let head_sha_changed = pr.last_head_sha.as_deref() != Some(obs.head_sha.as_str());
                if head_sha_changed {
                    head_sha_updates.push((*pr_number, obs.head_sha.clone()));
                }
                if pr.review_state != obs.review_state {
                    review_state_updates.push((
                        *pr_number,
                        obs.review_state.clone(),
                        obs.head_sha.clone(),
                    ));
                }

                let actions = if let Some(old_state) = state_guard.get_mut(pr_number) {
                    if head_sha_changed {
                        old_state.reviewer_spawned = false;
                        if let Some(spawner) = &self.reviewer_spawner {
                            let spawner = spawner.clone();
                            let pr_clone = pr.clone();
                            let pr_num = *pr_number;
                            let sha = obs.head_sha.clone();
                            tokio::spawn(async move {
                                info!(pr_number = pr_num, head_sha = %sha, "Spawning reviewer agent for new PR head SHA");
                                match spawner.spawn_reviewer_for_pr(&pr_clone).await {
                                    Ok(_) => info!(
                                        pr_number = pr_num,
                                        head_sha = %sha,
                                        "Reviewer agent spawned successfully for new PR head SHA"
                                    ),
                                    Err(e) => {
                                        warn!(pr_number = pr_num, head_sha = %sha, error = %e, "Failed to spawn reviewer for new PR head SHA")
                                    }
                                }
                            });
                            old_state.reviewer_spawned = true;
                        }
                    }
                    compute_pr_actions_with_context(
                        old_state,
                        PRNumber::new(*pr_number),
                        &obs.head_sha,
                        &obs.comments,
                        &local_reviews,
                        obs.ci_status,
                        pr.merge_blocked_on_ci,
                        pr.reviewer_agent.is_some(),
                        obs.review_file_seen,
                        obs.review_file_mtime,
                        branch.as_str(),
                        &|c, r| format_review_message(c, r),
                        self.policy.reviewer_max_rounds,
                        self.policy.reviewer_max_wait_seconds,
                    )
                } else {
                    state_guard.insert(
                        *pr_number,
                        WatchState::new(&branch, agent_type, &obs.head_sha, CIStatus::Unknown, 0),
                    );
                    let actions = compute_pr_actions_with_context(
                        state_guard
                            .get_mut(pr_number)
                            .expect("watch state inserted above"),
                        PRNumber::new(*pr_number),
                        &obs.head_sha,
                        &obs.comments,
                        &local_reviews,
                        obs.ci_status,
                        pr.merge_blocked_on_ci,
                        pr.reviewer_agent.is_some(),
                        obs.review_file_seen,
                        obs.review_file_mtime,
                        branch.as_str(),
                        &|c, r| format_review_message(c, r),
                        self.policy.reviewer_max_rounds,
                        self.policy.reviewer_max_wait_seconds,
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
        if !review_state_updates.is_empty() {
            self.persist_review_states(&review_state_updates).await?;
            for (pr_number, review_state, _) in &review_state_updates {
                if matches!(
                    review_state,
                    LocalReviewState::Approved | LocalReviewState::ChangesRequested
                ) {
                    dispose_reviewers_for_pr(
                        self.ctx.project_dir(),
                        self.ctx.git_worktree_service().clone(),
                        *pr_number,
                    )
                    .await;
                }
            }
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
                            ReviewerFanOut::SuppressedSelfEcho => {
                                debug!(
                                    pr_number = pending.pr_number,
                                    kind = %payload_kind,
                                    "Suppressing reviewer self-echo"
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
                    PendingAction::WriteRegistryRounds { pr_number, rounds } => {
                        if let Err(e) = self.set_pr_rounds(pr_number, rounds).await {
                            warn!(pr_number, rounds, error = %e, "Failed to persist PR review rounds");
                        }
                    }
                    PendingAction::FileHumanEscalation {
                        pr_number,
                        classification,
                        diagnostic,
                    } => {
                        if let Err(e) = self
                            .file_review_loop_escalation(pr_number, classification, &diagnostic)
                            .await
                        {
                            warn!(
                                pr_number,
                                classification = classification.as_str(),
                                error = %e,
                                "Failed to file review-loop human escalation"
                            );
                        }
                    }
                    PendingAction::TriggerManualCi {
                        pr_number,
                        branch,
                        head_sha,
                    } => {
                        info!(pr_number, branch = %branch, head_sha = %head_sha, "Manual CI trigger is disabled until Forgejo integration is configured");
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
                    pr.approved_at_sha = None;
                    changed = true;
                }
            }
        }

        if changed {
            write_pr_registry(&self.prs_path, &registry).await?;
        }

        Ok(())
    }

    async fn persist_review_states(
        &self,
        updates: &[(u64, LocalReviewState, String)],
    ) -> Result<()> {
        let mut registry = read_pr_registry(&self.prs_path).await?;
        let mut changed = false;

        for (pr_number, review_state, head_sha) in updates {
            if let Some(pr) = registry.prs.get_mut(pr_number) {
                if pr.review_state != *review_state {
                    pr.review_state = review_state.clone();
                    if matches!(review_state, LocalReviewState::Approved) {
                        pr.approved_at_sha = Some(head_sha.clone());
                        pr.stuck = false;
                        pr.needs_human_review = false;
                    } else {
                        pr.approved_at_sha = None;
                    }
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

    async fn file_review_loop_escalation(
        &self,
        pr_number: u64,
        classification: ReviewStallKind,
        diagnostic: &ReviewStallDiagnostic,
    ) -> Result<()> {
        debug_assert!(REVIEW_STALL_KINDS.contains(&classification));
        let title = format!(
            "Fix review loop escalation for PR #{}: {}",
            pr_number,
            classification.title_fragment()
        );
        let description = format!(
            "Watcher classified PR #{} as `{}`.\n\n\
             This is routed to the human review-loop escalation surface, not to the TL. \
             The TL contract is intentionally limited to `[MERGE READY]`.\n\n\
             Diagnostic:\n\
             - branch: `{}`\n\
             - head_sha: `{}`\n\
             - last_observed_sha: `{}`\n\
             - rounds: `{}`\n\
             - reviewer_registered: `{}`\n\
             - review_file_seen: `{}`\n\
             - review_file_mtime: `{}`\n\
             - wait_seconds: `{}`\n\
             - ci_status: `{}`",
            pr_number,
            classification.as_str(),
            diagnostic.branch,
            diagnostic.head_sha,
            diagnostic.last_observed_sha,
            diagnostic.rounds,
            diagnostic.reviewer_registered,
            diagnostic.review_file_seen,
            diagnostic
                .review_file_mtime
                .as_deref()
                .unwrap_or("<never written>"),
            diagnostic.wait_seconds,
            diagnostic.ci_status
        );

        let output = Command::new("chainlink")
            .current_dir(self.ctx.project_dir())
            .args([
                "create",
                title.as_str(),
                "-p",
                "high",
                "-l",
                "review-stuck",
                "-d",
                description.as_str(),
            ])
            .output()
            .await
            .context("failed to run chainlink create for review-loop escalation")?;

        if !output.status.success() {
            anyhow::bail!(
                "chainlink create failed for review-loop escalation: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            );
        }

        info!(
            pr_number,
            classification = classification.as_str(),
            stdout = %String::from_utf8_lossy(&output.stdout).trim(),
            "Filed review-loop human escalation issue"
        );
        Ok(())
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
#[cfg(test)]
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
    compute_pr_actions_with_context(
        old_state,
        pr_number,
        pr_sha,
        comments,
        reviews,
        ci_status,
        merge_blocked_on_ci,
        false,
        false,
        None,
        branch,
        format_message,
        max_rounds,
        15 * 60,
    )
}

fn compute_pr_actions_with_context(
    old_state: &mut WatchState,
    pr_number: PRNumber,
    pr_sha: &str,
    comments: &[LocalReviewComment],
    reviews: &[LocalReview],
    ci_status: CIStatus,
    merge_blocked_on_ci: bool,
    reviewer_registered: bool,
    review_file_seen: bool,
    review_file_mtime: Option<SystemTime>,
    branch: &str,
    format_message: &dyn Fn(&[LocalReviewComment], &[LocalReview]) -> String,
    max_rounds: u32,
    max_wait_seconds: u64,
) -> Vec<PendingAction> {
    let mut pending_actions = Vec::new();
    let comment_count = comments.len() + reviews.len();

    let now = Instant::now();
    let ci_changed = ci_status != old_state.last_ci_status;
    let ci_now_mergeable = ci_status == CIStatus::Success || ci_status == CIStatus::Neutral;
    if ci_changed {
        old_state.ci_mergeable_at = if ci_now_mergeable { Some(now) } else { None };
    }
    let mut merge_ready_now = !old_state.merge_ready_notified
        && signals_within_merge_ready_window(
            old_state.review_approved_at,
            old_state.ci_mergeable_at,
        );
    let recover_after_ci_block = merge_blocked_on_ci && ci_changed && ci_now_mergeable;

    if pr_sha != old_state.last_sha {
        let was_changes_requested = old_state.last_review_state == ReviewState::ChangesRequested;
        old_state.last_sha = pr_sha.to_string();
        old_state.last_review_state = ReviewState::None;
        old_state.notified_parent_approved = false;
        old_state.notified_parent_timeout = false;
        old_state.review_approved_at = None;
        old_state.merge_ready_notified = false;
        old_state.ci_triggered_sha = None;
        old_state.ci_blocked_notified = false;
        old_state.first_seen = Instant::now();
        merge_ready_now = false;
        if was_changes_requested {
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

    if ci_changed
        && ci_status == CIStatus::Failure
        && old_state.review_approved_at.is_some()
        && !old_state.ci_blocked_notified
    {
        old_state.stuck = true;
        old_state.ci_blocked_notified = true;
        pending_actions.push(PendingAction::WriteRegistryStuck {
            pr_number: pr_number.as_u64(),
            rounds: old_state.rounds,
        });
        pending_actions.push(PendingAction::FileHumanEscalation {
            pr_number: pr_number.as_u64(),
            classification: ReviewStallKind::CiFailed,
            diagnostic: review_stall_diagnostic(
                old_state,
                pr_sha,
                branch,
                reviewer_registered,
                review_file_seen,
                review_file_mtime,
                max_wait_seconds,
                ci_status,
            ),
        });
        pending_actions.push(PendingAction::WasmEvent {
            event_type: "pr_review",
            payload: serde_json::json!({
                "kind": "ci_blocked",
                "pr_number": pr_number.as_u64(),
                "ci_status": ci_status.as_str(),
                "branch": branch,
            }),
        });
    }

    if (old_state.notified_parent_approved || old_state.notified_parent_timeout || old_state.stuck)
        && !recover_after_ci_block
        && !merge_ready_now
    {
        return pending_actions;
    }

    if comment_count != old_state.pr_review_cycle_count {
        old_state.pr_review_cycle_count = comment_count;
    }

    let observed_request_change_rounds = reviews
        .iter()
        .filter(|r| r.state == ReviewState::ChangesRequested)
        .count() as u32;
    let next_review_round = observed_request_change_rounds.max(old_state.rounds + 1);

    let approved = reviews
        .iter()
        .any(|r| r.state == ReviewState::Approved || r.body.to_lowercase().contains("approved"));
    if approved && old_state.last_review_state != ReviewState::Approved {
        let approved_round = if old_state.rounds == 0 {
            1
        } else if observed_request_change_rounds >= old_state.rounds {
            old_state.rounds
        } else {
            old_state.rounds + 1
        };
        old_state.rounds = approved_round;
        pending_actions.push(PendingAction::WriteRegistryRounds {
            pr_number: pr_number.as_u64(),
            rounds: old_state.rounds,
        });
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
            review_event_kind_for_state(&ReviewState::Approved)
                .expect("approved review state has an event kind")
        };
        if merge_ready_now {
            old_state.merge_ready_notified = true;
        } else if old_state.ci_triggered_sha.as_deref() != Some(pr_sha) {
            old_state.ci_triggered_sha = Some(pr_sha.to_string());
            pending_actions.push(PendingAction::TriggerManualCi {
                pr_number: pr_number.as_u64(),
                branch: branch.to_string(),
                head_sha: pr_sha.to_string(),
            });
            pending_actions.push(PendingAction::WasmEvent {
                event_type: "pr_review",
                payload: serde_json::json!({
                    "kind": "ci_triggered",
                    "pr_number": pr_number.as_u64(),
                    "branch": branch,
                    "head_sha": pr_sha,
                }),
            });
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
    if !approved
        && changes_requested
        && old_state.last_review_state != ReviewState::ChangesRequested
    {
        old_state.last_review_state = ReviewState::ChangesRequested;
        old_state.first_seen = now;
        old_state.rounds = next_review_round;

        if old_state.rounds > max_rounds {
            old_state.stuck = true;
            pending_actions.push(PendingAction::WriteRegistryStuck {
                pr_number: pr_number.as_u64(),
                rounds: old_state.rounds,
            });
            pending_actions.push(PendingAction::FileHumanEscalation {
                pr_number: pr_number.as_u64(),
                classification: ReviewStallKind::DevNotPushing,
                diagnostic: review_stall_diagnostic(
                    old_state,
                    pr_sha,
                    branch,
                    reviewer_registered,
                    review_file_seen,
                    review_file_mtime,
                    max_wait_seconds,
                    ci_status,
                ),
            });
        } else {
            pending_actions.push(PendingAction::WriteRegistryRounds {
                pr_number: pr_number.as_u64(),
                rounds: old_state.rounds,
            });
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
                    "kind": review_event_kind_for_state(&ReviewState::ChangesRequested)
                        .expect("changes_requested review state has an event kind"),
                    "pr_number": pr_number.as_u64(),
                    "comments": message,
                    "author_branch": review_author_branch(reviews),
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

    if !old_state.notified_parent_timeout
        && !old_state.notified_parent_approved
        && old_state.first_seen.elapsed() > Duration::from_secs(max_wait_seconds)
    {
        let classification =
            classify_review_stall(old_state, reviewer_registered, review_file_seen);
        old_state.notified_parent_timeout = true;
        pending_actions.push(PendingAction::FileHumanEscalation {
            pr_number: pr_number.as_u64(),
            classification,
            diagnostic: review_stall_diagnostic(
                old_state,
                pr_sha,
                branch,
                reviewer_registered,
                review_file_seen,
                review_file_mtime,
                max_wait_seconds,
                ci_status,
            ),
        });
    }

    pending_actions
}

#[allow(dead_code)]
fn review_file_pr_number(file_name: &str) -> Option<u64> {
    file_name
        .strip_prefix("pr_")?
        .strip_suffix(".json")?
        .parse()
        .ok()
}

#[allow(dead_code)]
fn pr_entry_from_tangled_response(
    response: LocalPrResponse,
    review_file: Option<&ReviewFile>,
) -> PrEntry {
    let review_state = local_review_state_from_str(&response.review_state);
    let last_head_sha = non_empty(response.last_head_sha);
    let reviewer_agent = non_empty(response.reviewer_agent);
    let approved_at_sha = if matches!(review_state, LocalReviewState::Approved) {
        last_head_sha.clone()
    } else {
        None
    };
    let head_branch = response.head_branch;
    let author_agent = author_agent_from_branch(&head_branch)
        .or_else(|| non_empty(response.author_agent))
        .unwrap_or_else(|| format!("pr-{}", response.pr_number));
    let rounds = review_file.map(review_rounds_from_file).unwrap_or(0);

    PrEntry {
        number: response.pr_number as u64,
        head_branch,
        base_branch: response.base_branch,
        title: format!("Recovered PR #{}", response.pr_number),
        body: "Recovered from Tangled PR state and existing .exo/reviews verdicts.".to_string(),
        author_agent,
        author_role: "dev".to_string(),
        created_at: Utc::now(),
        state: PrState::Open,
        review_state,
        last_review_at: None,
        last_head_sha,
        approved_at_sha,
        reviewer_agent: reviewer_agent.clone(),
        reviewer_birth_branch: reviewer_agent
            .as_deref()
            .and_then(reviewer_birth_branch_from_agent),
        rounds,
        stuck: false,
        needs_human_review: false,
        merge_blocked_on_ci: false,
        chainlink_issue_id: None,
    }
}

#[allow(dead_code)]
fn non_empty(value: String) -> Option<String> {
    if value.is_empty() {
        None
    } else {
        Some(value)
    }
}

#[allow(dead_code)]
fn author_agent_from_branch(branch: &str) -> Option<String> {
    branch
        .rsplit_once('.')
        .map(|(_, slug)| slug.to_string())
        .filter(|slug| !slug.is_empty())
}

#[allow(dead_code)]
fn reviewer_birth_branch_from_agent(agent: &str) -> Option<String> {
    let rest = agent.strip_prefix("review-pr-")?;
    let pr_digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    if pr_digits.is_empty() {
        None
    } else {
        Some(format!("review-pr-{pr_digits}"))
    }
}

#[allow(dead_code)]
fn review_rounds_from_file(review_file: &ReviewFile) -> u32 {
    let mut shas = std::collections::HashSet::new();
    for verdict in &review_file.verdicts {
        if let Some(sha) = verdict.head_sha.as_deref() {
            if !sha.is_empty() {
                shas.insert(sha);
            }
        }
    }
    if !shas.is_empty() {
        return shas.len() as u32;
    }
    if review_file.state == "approved" || review_file.state == "changes_requested" {
        1
    } else {
        0
    }
}

fn review_file_parts(
    rf: ReviewFile,
    current_head_sha: Option<&str>,
) -> (LocalReviewState, Vec<LocalReviewComment>, Vec<LocalReview>) {
    let comments: Vec<LocalReviewComment> = rf
        .comments
        .into_iter()
        .map(review_comment_entry_to_local)
        .collect();
    if rf.verdicts.is_empty() {
        let state = local_review_state_from_str(&rf.state);
        let reviews = legacy_reviews_from_state(&state, &comments);
        return (state, comments, reviews);
    }

    let has_sha_scoped_verdict = rf.verdicts.iter().any(|verdict| verdict.head_sha.is_some());
    let verdicts: Vec<ReviewVerdictEntry> = if has_sha_scoped_verdict {
        current_head_sha
            .map(|sha| {
                rf.verdicts
                    .into_iter()
                    .filter(|verdict| verdict.head_sha.as_deref() == Some(sha))
                    .collect()
            })
            .unwrap_or_default()
    } else {
        rf.verdicts
    };

    if verdicts.is_empty() {
        return (LocalReviewState::PendingReview, vec![], vec![]);
    }

    let state = verdicts
        .last()
        .map(|verdict| local_review_state_from_str(&verdict.state))
        .unwrap_or(LocalReviewState::PendingReview);
    let reviews = verdicts
        .into_iter()
        .map(|verdict| {
            let body = if verdict.body.is_empty() {
                format_review_message(
                    &verdict
                        .comments
                        .iter()
                        .cloned()
                        .map(review_comment_entry_to_local)
                        .collect::<Vec<_>>(),
                    &[],
                )
            } else {
                verdict.body
            };
            LocalReview {
                body,
                state: review_state_from_str(&verdict.state),
                author_branch: verdict.author_branch,
            }
        })
        .collect();

    (state, comments, reviews)
}

fn review_comment_entry_to_local(c: ReviewCommentEntry) -> LocalReviewComment {
    LocalReviewComment {
        body: c.body,
        path: c.path,
        diff_hunk: c.diff_hunk,
        thread_id: c.thread_id,
        resolved: c.resolved,
        author_branch: c.author_branch,
    }
}

fn local_review_state_from_str(state: &str) -> LocalReviewState {
    match state {
        "approved" => LocalReviewState::Approved,
        "changes_requested" => LocalReviewState::ChangesRequested,
        _ => LocalReviewState::PendingReview,
    }
}

fn review_state_from_str(state: &str) -> ReviewState {
    match state {
        "approved" => ReviewState::Approved,
        "changes_requested" => ReviewState::ChangesRequested,
        _ => ReviewState::None,
    }
}

fn review_event_kind_for_state(state: &ReviewState) -> Option<&'static str> {
    match state {
        ReviewState::Approved => Some("approved"),
        ReviewState::ChangesRequested => Some("review_received"),
        ReviewState::None => None,
    }
}

fn legacy_reviews_from_state(
    state: &LocalReviewState,
    comments: &[LocalReviewComment],
) -> Vec<LocalReview> {
    comments
        .iter()
        .map(|comment| LocalReview {
            body: comment.body.clone(),
            state: match state {
                LocalReviewState::Approved => ReviewState::Approved,
                LocalReviewState::ChangesRequested => ReviewState::ChangesRequested,
                LocalReviewState::PendingReview => ReviewState::None,
            },
            author_branch: comment.author_branch.clone(),
        })
        .collect()
}

fn obs_to_review_parts(obs: &Observation) -> (Vec<LocalReview>, ReviewState) {
    let state = match obs.review_state {
        LocalReviewState::Approved => ReviewState::Approved,
        LocalReviewState::ChangesRequested => ReviewState::ChangesRequested,
        LocalReviewState::PendingReview => ReviewState::None,
    };

    if !obs.reviews.is_empty() {
        return (obs.reviews.clone(), state);
    }

    let mut reviews: Vec<LocalReview> = obs
        .comments
        .iter()
        .map(|c| LocalReview {
            body: c.body.clone(),
            state: state.clone(),
            author_branch: c.author_branch.clone(),
        })
        .collect();

    if obs.review_state == LocalReviewState::Approved && reviews.is_empty() {
        reviews.push(LocalReview {
            body: "Approved".to_string(),
            state: ReviewState::Approved,
            author_branch: None,
        });
    } else if obs.review_state == LocalReviewState::ChangesRequested && reviews.is_empty() {
        reviews.push(LocalReview {
            body: "Changes requested".to_string(),
            state: ReviewState::ChangesRequested,
            author_branch: None,
        });
    }

    (reviews, state)
}

fn review_author_branch(reviews: &[LocalReview]) -> Option<&str> {
    reviews
        .iter()
        .rev()
        .find(|review| review.state == ReviewState::ChangesRequested)
        .and_then(|review| review.author_branch.as_deref())
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

fn classify_review_stall(
    state: &WatchState,
    reviewer_registered: bool,
    review_file_seen: bool,
) -> ReviewStallKind {
    if state.last_review_state == ReviewState::ChangesRequested {
        return ReviewStallKind::DevNotPushing;
    }

    if state.addressed_changes && review_file_seen {
        return ReviewStallKind::ReviewerNotResponding;
    }

    if reviewer_registered && !review_file_seen {
        return ReviewStallKind::ReviewerNeverStarted;
    }

    ReviewStallKind::ReviewerNotResponding
}

fn review_stall_diagnostic(
    state: &WatchState,
    head_sha: &str,
    branch: &str,
    reviewer_registered: bool,
    review_file_seen: bool,
    review_file_mtime: Option<SystemTime>,
    wait_seconds: u64,
    ci_status: CIStatus,
) -> ReviewStallDiagnostic {
    ReviewStallDiagnostic {
        branch: branch.to_string(),
        head_sha: head_sha.to_string(),
        last_observed_sha: state.last_sha.clone(),
        rounds: state.rounds,
        reviewer_registered,
        review_file_seen,
        review_file_mtime: review_file_mtime.map(|mtime| format!("{mtime:?}")),
        wait_seconds,
        ci_status: ci_status.to_string(),
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

fn ci_status_key(branch: &BranchName, head_sha: &str) -> CiStatusKey {
    (branch.clone(), head_sha.to_string())
}

/// Extracts the branch name and head SHA from a `sh.tangled.pipeline` event payload.
/// Returns `None` if the trigger is not a push or is missing the ref/SHA fields.
#[allow(dead_code)]
fn extract_pipeline_branch_and_sha(event: &serde_json::Value) -> Option<(BranchName, String)> {
    let push = event.get("triggerMetadata").and_then(|tm| tm.get("push"))?;
    let ref_str = push.get("ref").and_then(|r| r.as_str())?;
    let head_sha = push.get("newSha").and_then(|sha| sha.as_str())?;

    let branch = ref_str.strip_prefix("refs/heads/").unwrap_or(ref_str);
    if branch.is_empty() || head_sha.is_empty() {
        None
    } else {
        Some((
            BranchName::try_from_str(branch).expect("validated string input is non-empty"),
            head_sha.to_string(),
        ))
    }
}

/// Extracts the pipeline rkey and status string from a `sh.tangled.pipeline.status` event payload.
/// The pipeline field is an AT-URI; the rkey is the last path segment.
#[allow(dead_code)]
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
            author_branch: None,
        }
    }

    fn test_review(body: &str, state: ReviewState) -> LocalReview {
        LocalReview {
            body: body.to_string(),
            state,
            author_branch: None,
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
    fn test_new_sha_after_approval_reopens_review_round() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.last_review_state = ReviewState::Approved;
        state.notified_parent_approved = true;
        state.review_approved_at = Some(Instant::now());

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

        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. } if payload["kind"] == "commits_pushed"
        )));
        assert_eq!(state.last_sha, "def456");
        assert_eq!(state.last_review_state, ReviewState::None);
        assert!(!state.notified_parent_approved);
        assert_eq!(state.review_approved_at, None);
    }

    #[test]
    fn test_first_approval_increments_review_rounds() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.ci_mergeable_at = Some(Instant::now());
        let reviews = vec![test_review("approved", ReviewState::Approved)];

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

        assert_eq!(state.rounds, 1);
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WriteRegistryRounds {
                pr_number: 1,
                rounds: 1
            }
        )));
    }

    #[test]
    fn test_approval_after_new_sha_increments_review_rounds_once() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.rounds = 1;
        state.last_review_state = ReviewState::Approved;
        state.notified_parent_approved = true;
        state.review_approved_at = Some(Instant::now());

        let _ = compute_pr_actions(
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
        let reviews = vec![test_review("approved", ReviewState::Approved)];
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "def456",
            &[],
            &reviews,
            CIStatus::Success,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );

        assert_eq!(state.rounds, 2);
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WriteRegistryRounds {
                pr_number: 1,
                rounds: 2
            }
        )));
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

    #[test]
    fn test_reviewer_approval_triggers_manual_ci_when_status_unknown() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review("approved", ReviewState::Approved)];

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

        assert!(matches!(
            actions.iter().find(|action| matches!(action, PendingAction::TriggerManualCi { .. })),
            Some(PendingAction::TriggerManualCi { pr_number: 1, branch, head_sha })
                if branch == "main.feat-gemini" && head_sha == "abc123"
        ));
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. }
                if payload["kind"] == "ci_triggered" && payload["head_sha"] == "abc123"
        )));
        assert_eq!(state.ci_triggered_sha.as_deref(), Some("abc123"));
    }

    #[test]
    fn test_ci_failure_after_approval_blocks_pr() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.last_ci_status = CIStatus::Pending;
        state.last_review_state = ReviewState::Approved;
        state.notified_parent_approved = true;
        state.review_approved_at = Some(Instant::now());
        state.rounds = 1;

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Failure,
            true,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );

        assert!(state.stuck);
        assert!(state.ci_blocked_notified);
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::FileHumanEscalation {
                classification: ReviewStallKind::CiFailed,
                ..
            }
        )));
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. }
                if payload["kind"] == "ci_blocked" && payload["ci_status"] == "failure"
        )));
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
    fn test_reviewer_fanout_decision_suppresses_self_authored_review_received() {
        let registry = test_registry(pr_with_reviewer(1, "review-pr-1-codex", "review-pr-1"));
        let payload = serde_json::json!({
            "kind": "review_received",
            "pr_number": 1,
            "author_branch": "review-pr-1",
        });

        assert_eq!(
            reviewer_fanout_decision("pr_review", &payload, 1, &registry),
            ReviewerFanOut::SuppressedSelfEcho
        );
    }

    #[test]
    fn test_reviewer_fanout_decision_dispatches_review_from_other_author() {
        let registry = test_registry(pr_with_reviewer(1, "review-pr-1-codex", "review-pr-1"));
        let payload = serde_json::json!({
            "kind": "review_received",
            "pr_number": 1,
            "author_branch": "human-reviewer",
        });

        match reviewer_fanout_decision("pr_review", &payload, 1, &registry) {
            ReviewerFanOut::DispatchTo(branch, _, _) => assert_eq!(branch.as_str(), "review-pr-1"),
            other => panic!("expected DispatchTo, got {other:?}"),
        }
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
    fn test_new_comments_update_comment_count_without_review_received() {
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
        assert!(!actions
            .iter()
            .any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "review_received")));
        assert_eq!(state.pr_review_cycle_count, 1);
    }

    #[test]
    fn test_changes_requested_fires_single_review_received() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let comments = vec![test_comment("Fix this")];
        let reviews = vec![test_review(
            "Please address comments",
            ReviewState::ChangesRequested,
        )];

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &comments,
            &reviews,
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, _| "review message".to_string(),
            5,
        );

        let review_received_count = actions
            .iter()
            .filter(|a| {
                matches!(a, PendingAction::WasmEvent { payload, .. }
                if payload["kind"] == "review_received")
            })
            .count();
        let emit_event_count = actions
            .iter()
            .filter(|a| matches!(a, PendingAction::EmitEvent { .. }))
            .count();

        assert_eq!(review_received_count, 1);
        assert_eq!(emit_event_count, 1);
        assert_eq!(state.pr_review_cycle_count, 2);
        assert_eq!(state.last_review_state, ReviewState::ChangesRequested);
    }

    #[test]
    fn test_changes_requested_payload_records_review_author_branch() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![LocalReview {
            body: "Please address comments".to_string(),
            state: ReviewState::ChangesRequested,
            author_branch: Some("review-pr-1".to_string()),
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
            &|_, _| "review message".to_string(),
            5,
        );

        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. }
                if payload["kind"] == "review_received"
                    && payload["author_branch"] == "review-pr-1"
        )));
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
    fn test_initial_approved_neutral_ci_observation_fires_merge_ready() {
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
            CIStatus::Neutral,
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
            } if payload["kind"] == "merge_ready" && payload["ci_status"] == "neutral"
        )));
        assert!(state.merge_ready_notified);
    }

    #[tokio::test]
    async fn test_default_ci_gate_is_neutral_without_ci_source() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();
        let watcher = WorktreeEventWatcher::new(Arc::new(services));
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");

        assert_eq!(
            watcher.observed_ci_status(&branch, "abc123").await,
            CIStatus::Neutral
        );
    }

    #[tokio::test]
    async fn test_ci_gate_uses_unknown_when_source_configured_without_status() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();
        let watcher = WorktreeEventWatcher::new(Arc::new(services));
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");

        assert_eq!(
            watcher.observed_ci_status(&branch, "abc123").await,
            CIStatus::Unknown
        );
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
        state.rounds = 1;
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
        assert_eq!(state.rounds, 2);
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WriteRegistryStuck {
                pr_number: 1,
                rounds: 2,
            }
        )));
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::FileHumanEscalation {
                pr_number: 1,
                classification: ReviewStallKind::DevNotPushing,
                ..
            },
        )));
    }

    #[test]
    fn test_request_changes_then_approve_does_not_trip_stuck() {
        let branch = BranchName::try_from_str("main.feat-codex")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Codex, "abc123");
        let request_changes = vec![test_review(
            "Add required header",
            ReviewState::ChangesRequested,
        )];

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &request_changes,
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, _| "Add required header".to_string(),
            2,
        );

        assert_eq!(state.rounds, 1);
        assert!(!state.stuck);
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. } if payload["kind"] == "review_received"
        )));

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
            2,
        );

        assert_eq!(state.rounds, 1);
        assert!(!state.stuck);
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. } if payload["kind"] == "fixes_pushed"
        )));

        let approved = vec![
            test_review("Add required header", ReviewState::ChangesRequested),
            test_review("Approved", ReviewState::Approved),
        ];
        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "def456",
            &[],
            &approved,
            CIStatus::Success,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            2,
        );

        assert_eq!(state.rounds, 1);
        assert!(!state.stuck);
        assert_eq!(state.last_review_state, ReviewState::Approved);
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. }
                if payload["kind"] == "approved" || payload["kind"] == "merge_ready"
        )));
        assert!(!actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. } if payload["kind"] == "stuck"
        )));
    }

    #[test]
    fn test_request_changes_history_preserves_round_when_poll_sees_only_approval() {
        let branch = BranchName::try_from_str("main.feat-codex")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Codex, "def456");
        let reviews = vec![
            test_review("Add required header", ReviewState::ChangesRequested),
            test_review("Approved", ReviewState::Approved),
        ];

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "def456",
            &[],
            &reviews,
            CIStatus::Success,
            false,
            branch.as_str(),
            &|_, _| String::new(),
            2,
        );

        assert_eq!(state.rounds, 1);
        assert!(!state.stuck);
        assert_eq!(state.last_review_state, ReviewState::Approved);
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WriteRegistryRounds {
                pr_number: 1,
                rounds: 1,
            }
        )));
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. }
                if payload["kind"] == "approved" || payload["kind"] == "merge_ready"
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
        assert!(actions.iter().any(|a| matches!(
            a,
            PendingAction::FileHumanEscalation {
                classification: ReviewStallKind::ReviewerNotResponding,
                ..
            }
        )));
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
            "abc123",
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
    fn test_stale_guard_suppresses_after_stuck() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.stuck = true;
        state.rounds = 2;
        let reviews = vec![test_review("Late approval", ReviewState::Approved)];

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
            2,
        );

        assert!(actions.is_empty());
        assert!(state.stuck);
        assert!(!state.notified_parent_approved);
        assert_eq!(state.last_sha, "abc123");
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
            author_branch: None,
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
        let actions = compute_pr_actions_with_context(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &[],
            CIStatus::Unknown,
            false,
            true,
            true,
            None,
            branch.as_str(),
            &|_, _| String::new(),
            5,
            5 * 60,
        );
        assert!(actions
            .iter()
            .any(|a| matches!(a, PendingAction::FileHumanEscalation {
                  classification: ReviewStallKind::ReviewerNotResponding,
                  diagnostic,
                  ..
              } if diagnostic.wait_seconds == 5 * 60)));
    }

    #[test]
    fn test_review_stall_classification_names_stuck_actor() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");

        state.last_review_state = ReviewState::ChangesRequested;
        assert_eq!(
            classify_review_stall(&state, true, true),
            ReviewStallKind::DevNotPushing
        );

        state.last_review_state = ReviewState::None;
        state.addressed_changes = true;
        assert_eq!(
            classify_review_stall(&state, true, true),
            ReviewStallKind::ReviewerNotResponding
        );

        state.addressed_changes = false;
        assert_eq!(
            classify_review_stall(&state, true, false),
            ReviewStallKind::ReviewerNeverStarted
        );

        assert_eq!(ReviewStallKind::DevFailed.as_str(), "dev_failed");
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
            reviews: vec![],
            ci_status: CIStatus::Unknown,
            review_file_seen: false,
            review_file_mtime: None,
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
            reviews: vec![],
            ci_status: CIStatus::Unknown,
            review_file_seen: false,
            review_file_mtime: None,
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
            reviews: vec![],
            ci_status: CIStatus::Unknown,
            review_file_seen: false,
            review_file_mtime: None,
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
                author_branch: None,
            },
            LocalReview {
                body: "Good work.".to_string(),
                state: ReviewState::None,
                author_branch: None,
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
            author_branch: None,
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

    #[test]
    fn test_review_file_with_verdict_history_counts_request_changes() {
        let json = r#"{
            "state": "approved",
            "comments": [],
            "verdicts": [
                {
                    "state": "changes_requested",
                    "body": "Needs tests",
                    "comments": [{"body": "Add coverage", "path": "src/lib.rs"}]
                },
                {"state": "approved", "body": "LGTM", "comments": []}
            ]
        }"#;
        let rf: ReviewFile = serde_json::from_str(json).unwrap();
        let (state, comments, reviews) = review_file_parts(rf, Some("abc123"));

        assert_eq!(state, LocalReviewState::Approved);
        assert!(comments.is_empty());
        assert_eq!(
            reviews
                .iter()
                .filter(|review| review.state == ReviewState::ChangesRequested)
                .count(),
            1
        );
        assert!(reviews
            .iter()
            .any(|review| review.state == ReviewState::Approved && review.body == "LGTM"));
    }

    #[test]
    fn test_review_file_filters_sha_scoped_verdicts_to_current_head() {
        let json = r#"{
            "state": "approved",
            "comments": [],
            "verdicts": [
                {"state": "approved", "body": "old", "comments": [], "head_sha": "abc123"},
                {"state": "changes_requested", "body": "current", "comments": [], "head_sha": "def456"}
            ]
        }"#;
        let rf: ReviewFile = serde_json::from_str(json).unwrap();
        let (state, comments, reviews) = review_file_parts(rf, Some("def456"));

        assert_eq!(state, LocalReviewState::ChangesRequested);
        assert!(comments.is_empty());
        assert_eq!(reviews.len(), 1);
        assert_eq!(reviews[0].body, "current");
    }

    #[test]
    fn test_review_file_without_current_sha_is_pending_review() {
        let json = r#"{
            "state": "approved",
            "comments": [],
            "verdicts": [
                {"state": "approved", "body": "old", "comments": [], "head_sha": "abc123"}
            ]
        }"#;
        let rf: ReviewFile = serde_json::from_str(json).unwrap();
        let (state, comments, reviews) = review_file_parts(rf, Some("def456"));

        assert_eq!(state, LocalReviewState::PendingReview);
        assert!(comments.is_empty());
        assert!(reviews.is_empty());
    }

    #[test]
    fn test_review_state_dispatch_kind_mapping() {
        assert_eq!(
            review_event_kind_for_state(&ReviewState::ChangesRequested),
            Some("review_received")
        );
        assert_eq!(
            review_event_kind_for_state(&ReviewState::Approved),
            Some("approved")
        );
        assert_eq!(review_event_kind_for_state(&ReviewState::None), None);
    }

    #[test]
    fn test_review_file_state_drives_expected_pr_review_kind() {
        let branch = BranchName::try_from_str("main.feat-codex")
            .expect("literal validated string is non-empty");

        for (state, expected_kind) in [
            ("changes_requested", Some("review_received")),
            ("approved", Some("approved")),
            ("none", None),
        ] {
            let json = format!(r#"{{"state": "{state}", "comments": []}}"#);
            let rf: ReviewFile = serde_json::from_str(&json).unwrap();
            let (review_state, comments, reviews) = review_file_parts(rf, Some("abc123"));
            let obs = Observation {
                head_sha: "abc123".to_string(),
                review_state,
                comments,
                reviews,
                ci_status: CIStatus::Unknown,
                review_file_seen: true,
                review_file_mtime: None,
            };
            let (reviews, _) = obs_to_review_parts(&obs);
            let mut watcher_state = test_state(&branch, AgentType::Codex, "abc123");
            let actions = compute_pr_actions(
                &mut watcher_state,
                PRNumber::new(1),
                "abc123",
                &obs.comments,
                &reviews,
                CIStatus::Unknown,
                false,
                branch.as_str(),
                &|_, _| "review message".to_string(),
                2,
            );
            let observed_kinds: Vec<&str> = actions
                .iter()
                .filter_map(|action| match action {
                    PendingAction::WasmEvent {
                        event_type: "pr_review",
                        payload,
                    } => payload.get("kind").and_then(|kind| kind.as_str()),
                    _ => None,
                })
                .collect();

            match expected_kind {
                Some(kind) => assert!(
                    observed_kinds.contains(&kind),
                    "state {state}: expected {kind:?} in {observed_kinds:?}"
                ),
                None => assert!(
                    observed_kinds.is_empty(),
                    "state {state}: expected no event, got {observed_kinds:?}"
                ),
            }
        }
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
            approved_at_sha: None,
            reviewer_agent: None,
            reviewer_birth_branch: None,
            rounds: 0,
            stuck: false,
            needs_human_review: false,
            merge_blocked_on_ci: false,
            chainlink_issue_id: None,
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
            reviews: vec![],
            ci_status: CIStatus::Unknown,
            review_file_seen: false,
            review_file_mtime: None,
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
    async fn test_process_observations_persists_approved_review_state() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services));
        let mut pr = test_pr_entry();
        pr.review_state = crate::services::file_pr_local::LocalReviewState::PendingReview;
        pr.stuck = true;
        pr.needs_human_review = true;
        let registry = test_registry(pr);
        write_pr_registry(&watcher.prs_path, &registry)
            .await
            .unwrap();

        let mut observations = HashMap::new();
        observations.insert(
            1u64,
            Observation {
                head_sha: "abc123".to_string(),
                review_state: crate::services::file_pr_local::LocalReviewState::Approved,
                comments: vec![],
                reviews: vec![],
                ci_status: CIStatus::Unknown,
                review_file_seen: false,
                review_file_mtime: None,
            },
        );

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();

        let persisted = read_pr_registry(&watcher.prs_path).await.unwrap();
        let pr = persisted.prs.get(&1).unwrap();
        assert_eq!(
            pr.review_state,
            crate::services::file_pr_local::LocalReviewState::Approved
        );
        assert_eq!(pr.approved_at_sha.as_deref(), Some("abc123"));
        assert!(!pr.stuck);
        assert!(!pr.needs_human_review);
    }

    #[tokio::test]
    async fn test_process_observations_clears_approved_sha_on_new_head() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services));
        let mut pr = test_pr_entry();
        pr.review_state = crate::services::file_pr_local::LocalReviewState::Approved;
        pr.last_head_sha = Some("abc123".to_string());
        pr.approved_at_sha = Some("abc123".to_string());
        let registry = test_registry(pr);
        write_pr_registry(&watcher.prs_path, &registry)
            .await
            .unwrap();

        let mut observations = HashMap::new();
        observations.insert(
            1u64,
            Observation {
                head_sha: "def456".to_string(),
                review_state: crate::services::file_pr_local::LocalReviewState::Approved,
                comments: vec![],
                reviews: vec![],
                ci_status: CIStatus::Unknown,
                review_file_seen: false,
                review_file_mtime: None,
            },
        );

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();

        let persisted = read_pr_registry(&watcher.prs_path).await.unwrap();
        let pr = persisted.prs.get(&1).unwrap();
        assert_eq!(pr.last_head_sha.as_deref(), Some("def456"));
        assert_eq!(pr.approved_at_sha, None);
    }

    #[tokio::test]
    async fn test_observed_ci_status_is_sha_keyed() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let ci_status_map = services.ci_status_map.clone();
        ci_status_map
            .write()
            .await
            .insert((branch.clone(), "abc123".to_string()), CIStatus::Success);
        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_ci_status_map(ci_status_map);

        assert_eq!(
            watcher.observed_ci_status(&branch, "abc123").await,
            CIStatus::Success
        );
        assert_eq!(
            watcher.observed_ci_status(&branch, "def456").await,
            CIStatus::Unknown
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
    async fn test_reviewer_spawner_called_for_new_head_sha() {
        use std::sync::atomic::{AtomicUsize, Ordering};

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

        let call_count = Arc::new(AtomicUsize::new(0));
        let spawner = Arc::new(CountingSpawner {
            count: call_count.clone(),
        });

        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_reviewer_spawner(spawner);
        let branch = BranchName::try_from_str("main.feature-codex")
            .expect("literal validated string is non-empty");
        watcher.state.lock().await.insert(
            1,
            WatchState::new(&branch, AgentType::Codex, "abc123", CIStatus::Unknown, 0),
        );

        let mut pr = test_pr_entry();
        pr.last_head_sha = Some("abc123".to_string());
        let registry = test_registry(pr);
        let mut observations = HashMap::new();
        observations.insert(1u64, test_observation("def456"));

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        assert_eq!(call_count.load(Ordering::SeqCst), 1);
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
        let mut pr = test_pr_entry();
        pr.last_head_sha = Some("abc123".to_string());
        let registry = test_registry(pr);
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
