use crate::domain::{AgentName, BirthBranch, BranchName, CIStatus, PRNumber};
use crate::plugin_manager::PluginManager;
use crate::services::agent_control::AgentType;
use crate::services::agent_resources::dispose_reviewers_for_pr;
use crate::services::pr_registry::{ForgejoReviewState, PrEntry, PrRegistry, PrState};
use crate::services::repo;
use crate::services::review_policy::ReviewPolicy;
use crate::services::{
    CiStatusMap, HasAgentResolver, HasEventLog, HasEventQueue, HasForgejoClient,
    HasGitWorktreeService, HasInboxStore, HasProjectDir, HasTeamRegistry, ReviewerSpawner,
};
use anyhow::{Context, Result};
use chrono::Utc;
use exomonad_proto::effects::events::{event::EventType, AgentMessage, Event};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeSet, HashMap};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::process::Command;
use tokio::sync::{Mutex, RwLock};
use tracing::{debug, info, instrument, warn};

type PluginMap = Arc<RwLock<HashMap<AgentName, Arc<PluginManager>>>>;
const DEFAULT_INBOX_POKE_INTERVAL: Duration = Duration::from_secs(30);
const MAX_INBOX_POKE_INTERVAL: Duration = Duration::from_secs(600);

fn inbox_poke_message(unread_count: usize) -> String {
    format!(
        "You have {} unread message(s). Call check_inbox.",
        unread_count
    )
}
#[cfg(test)]
const MERGE_READY_SIGNAL_WINDOW: Duration = Duration::from_secs(30 * 60);

/// Overall verdict derived from Forgejo reviews for a single open PR.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
enum ForgejoReviewVerdict {
    #[default]
    None,
    Commented,
    ChangesRequested,
    Approved,
}

/// A review comment returned by Forgejo for an open PR.
#[derive(Debug, Clone, Serialize)]
struct ForgejoReviewComment {
    body: String,
    path: Option<String>,
    diff_hunk: Option<String>,
    thread_id: Option<String>,
    resolved: bool,
    author_branch: Option<String>,
}

/// A Forgejo review with a typed verdict.
#[derive(Debug, Clone, Serialize)]
struct ForgejoReview {
    review_id: Option<u64>,
    body: String,
    state: ForgejoReviewVerdict,
    author_branch: Option<String>,
    commit_id: Option<String>,
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
        comments: Option<Vec<ForgejoReviewComment>>,
        reviews: Option<Vec<ForgejoReview>>,
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

fn evict_closed_prs_from_state(state: &mut WatcherStateFile, registry: &PrRegistry) -> Vec<u64> {
    let mut evicted = Vec::new();
    state.prs.retain(|pr_number, _| {
        let keep = registry.prs.contains_key(pr_number);
        if !keep {
            evicted.push(*pr_number);
        }
        keep
    });
    evicted.sort_unstable();
    evicted
}

fn dropped_review_by_sha_log_line(pr_number: u64, review_commit: &str, head_sha: &str) -> String {
    format!(
        "dropped-review-by-SHA: PR #{pr_number} review commit {review_commit} does not match head {head_sha}"
    )
}

fn reviewer_disposal_log_line(pr_number: u64, reviewer_slugs: &[String]) -> String {
    if reviewer_slugs.is_empty() {
        format!("terminal review observed for PR #{pr_number} but no reviewer slug matched for disposal")
    } else {
        format!(
            "terminal review observed for PR #{pr_number}; disposing reviewer slugs: {}",
            reviewer_slugs.join(",")
        )
    }
}

fn review_event_target(pr: &PrEntry) -> (BranchName, AgentType, String) {
    (
        BranchName::try_from_str(pr.head_branch.as_str())
            .expect("validated string input is non-empty"),
        AgentType::from_dir_name(&pr.author_agent),
        pr.author_role.clone(),
    )
}

fn review_state_disposes_reviewer(review_state: &ForgejoReviewState) -> bool {
    matches!(
        review_state,
        ForgejoReviewState::Approved | ForgejoReviewState::ChangesRequested
    )
}

fn distinct_changes_requested_rounds(reviews: &[ForgejoReview]) -> u32 {
    reviews
        .iter()
        .filter(|review| review.state == ForgejoReviewVerdict::ChangesRequested)
        .map(|review| {
            format!(
                "{}\0{}\0{}",
                review.commit_id.as_deref().unwrap_or_default(),
                review.author_branch.as_deref().unwrap_or_default(),
                review.body
            )
        })
        .collect::<BTreeSet<_>>()
        .len() as u32
}

fn approved_review_round(old_rounds: u32, changes_requested_rounds: u32) -> u32 {
    if changes_requested_rounds > 0 {
        changes_requested_rounds.max(old_rounds)
    } else if old_rounds == 0 {
        1
    } else {
        old_rounds + 1
    }
}

fn should_spawn_reviewer_for_new_head(state: &WatchState, max_rounds: u32) -> bool {
    !state.reviewer_spawned && !state.reviewer_disposed && state.rounds < max_rounds
}

fn legacy_event_role_for_agent_type(agent_type: AgentType) -> &'static str {
    match agent_type {
        AgentType::Claude => "tl",
        AgentType::Gemini | AgentType::Shoal | AgentType::OpenCode | AgentType::Codex => "dev",
        AgentType::Process => "process",
    }
}

fn event_target_has_wasm_runtime(agent_type: AgentType) -> bool {
    matches!(agent_type, AgentType::Claude | AgentType::Gemini)
}

fn log_missing_event_plugin(
    branch: &str,
    agent_name: &AgentName,
    agent_type: AgentType,
    role: &str,
    event_type: &str,
) {
    if event_target_has_wasm_runtime(agent_type) {
        tracing::error!(
            branch,
            lookup_key = %agent_name,
            ?agent_type,
            role,
            event_type,
            "No plugin found for event target; skipping event dispatch"
        );
    } else {
        tracing::warn!(
            branch,
            lookup_key = %agent_name,
            ?agent_type,
            role,
            event_type,
            "No plugin found for non-WASM event target and no native handler matched; skipping event dispatch"
        );
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
    last_review_state: ForgejoReviewVerdict,
    last_review_fingerprint: Option<String>,
    last_sha: String,
    notified_parent_approved: bool,
    addressed_changes: bool,
    rounds: u32,
    stuck: bool,
    reviewer_spawned: bool,
    reviewer_disposed: bool,
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
}

#[derive(Debug, Clone, Serialize)]
struct ReviewStallDiagnostic {
    branch: String,
    head_sha: String,
    last_observed_sha: String,
    rounds: u32,
    reviewer_registered: bool,
    forgejo_review_present: bool,
    wait_seconds: u64,
    ci_status: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct WatcherStateFile {
    #[serde(default)]
    prs: HashMap<u64, WatcherPrState>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
struct WatcherPrState {
    #[serde(default)]
    rounds: u32,
    #[serde(default)]
    stuck: bool,
    #[serde(default)]
    needs_human_review: bool,
    #[serde(default)]
    last_review_state: ForgejoReviewVerdict,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    last_head_sha: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    last_review_fingerprint: Option<String>,
    #[serde(default)]
    notified_parent_timeout: bool,
    #[serde(default)]
    notified_parent_approved: bool,
    #[serde(default)]
    addressed_changes: bool,
    #[serde(default)]
    merge_ready_notified: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    ci_triggered_sha: Option<String>,
    #[serde(default)]
    ci_blocked_notified: bool,
}

#[derive(Debug, Default)]
struct PrBodyMetadata {
    author_agent: Option<String>,
    author_role: Option<String>,
    birth_branch: Option<String>,
    reviewer_agent: Option<String>,
    reviewer_birth_branch: Option<String>,
    chainlink_issue_id: Option<u64>,
}

fn parse_pr_body_metadata(body: &str) -> PrBodyMetadata {
    PrBodyMetadata {
        author_agent: pr_body_metadata_value(body, "Authoring-Agent"),
        author_role: pr_body_metadata_value(body, "Authoring-Role"),
        birth_branch: pr_body_metadata_value(body, "Birth-Branch"),
        reviewer_agent: pr_body_metadata_value(body, "Reviewer-Agent"),
        reviewer_birth_branch: pr_body_metadata_value(body, "Reviewer-Birth-Branch"),
        chainlink_issue_id: pr_body_metadata_value(body, "Chainlink-Issue")
            .and_then(|value| value.trim_start_matches('#').parse().ok()),
    }
}

fn pr_body_metadata_value(body: &str, key: &str) -> Option<String> {
    let prefix = format!("{key}:");
    body.lines()
        .find_map(|line| line.trim().strip_prefix(&prefix).map(str::trim))
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

fn author_agent_from_branch(branch: &str) -> Option<String> {
    branch
        .rsplit_once('.')
        .map(|(_, slug)| slug.to_string())
        .filter(|slug| !slug.is_empty())
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
            last_review_state: ForgejoReviewVerdict::None,
            last_review_fingerprint: None,
            last_sha: sha.to_string(),
            notified_parent_approved: false,
            addressed_changes: false,
            rounds: 0,
            stuck: false,
            reviewer_spawned: false,
            reviewer_disposed: false,
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

    fn reset_review_cycle(&mut self) {
        self.notified_parent_timeout = false;
        self.notified_parent_approved = false;
        self.merge_ready_notified = false;
        self.addressed_changes = false;
        self.rounds = 0;
        self.stuck = false;
        self.reviewer_spawned = false;
        self.reviewer_disposed = false;
        self.review_approved_at = None;
        self.ci_mergeable_at = None;
        self.ci_triggered_sha = None;
        self.ci_blocked_notified = false;
        self.last_review_fingerprint = None;
    }
}

#[derive(Debug, Default)]
pub struct WatcherRuntimeState {
    prs: Mutex<HashMap<u64, WatchState>>,
}

impl WatcherRuntimeState {
    pub fn new() -> Self {
        Self::default()
    }

    pub async fn reset_review_cycle(&self, pr_number: u64) -> bool {
        let mut state = self.prs.lock().await;
        let Some(pr_state) = state.get_mut(&pr_number) else {
            return false;
        };

        pr_state.reset_review_cycle();
        true
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

fn value_u64(payload: &serde_json::Value, key: &str) -> Option<u64> {
    payload.get(key).and_then(|value| value.as_u64())
}

fn value_i64(payload: &serde_json::Value, key: &str) -> Option<i64> {
    payload.get(key).and_then(|value| value.as_i64())
}

fn value_str<'a>(payload: &'a serde_json::Value, key: &str) -> Option<&'a str> {
    payload.get(key).and_then(|value| value.as_str())
}

fn review_parent_message(
    pr_number: u64,
    branch: &str,
    review_kind: &str,
    author_branch: Option<&str>,
    comments: &str,
) -> String {
    let reviewer_branch = author_branch.unwrap_or("unknown");
    format!(
        "[REVIEW ACTION REQUIRED] PR #{pr_number} on branch {branch}\nReview kind: {review_kind}\nReviewer branch: {reviewer_branch}\n\n{comments}\n\nTL action: spawn a fresh dev leaf to address this review and include the PR branch in its task."
    )
}

fn merge_ready_message(pr_number: u64, status: &str, branch: &str) -> String {
    format!(
        "[MERGE READY] PR #{pr_number} on branch {branch} has CI status {status} and reviewer approval. Merge with `merge_pr` tool."
    )
}

fn ci_status_message(pr_number: u64, status: &str, branch: &str) -> String {
    let suffix = match status {
        "success" => "\n\nCI passed.",
        "failure" => "\n\nCI failed. Check the logs and fix the issue before proceeding.",
        _ => "",
    };
    format!("[CI Status] PR #{pr_number} on branch {branch}: {status}{suffix}")
}

fn ci_blocked_message(pr_number: u64, status: &str, branch: &str) -> String {
    format!(
        "[CI BLOCKED: PR #{pr_number}] CI finished with status {status} on {branch}. Dev leaf is staying alive and waiting for TL direction."
    )
}

fn tl_ci_blocked_message(pr_number: u64, status: &str, branch: &str) -> String {
    format!(
        "[CI BLOCKED] PR #{pr_number} CI status {status} on {branch}. Human direction required."
    )
}

fn sibling_merged_message(merged_branch: &str, parent_branch: &str) -> String {
    format!(
        "[Sibling Merged] PR on branch {merged_branch} was merged into {parent_branch}. Rebase your branch to pick up the changes: git fetch origin && git rebase origin/{parent_branch}"
    )
}

fn pr_ready_message(pr_number: u64) -> String {
    format!("[PR READY] PR #{pr_number} approved by Forgejo reviewer. Merge with `merge_pr` tool.")
}

fn review_timeout_message(pr_number: u64, minutes: u64) -> String {
    format!(
        "[REVIEW TIMEOUT] PR #{pr_number} - no Forgejo reviewer response after {minutes} minutes. Merge with `merge_pr` using `force: true`."
    )
}

fn fixes_pushed_message(pr_number: u64, status: &str) -> String {
    let suffix = match status {
        "success" => " CI passing. Ready to merge.",
        "pending" => " CI running - merge when green.",
        _ => {
            return format!(
                "[FIXES PUSHED] PR #{pr_number} - review comments addressed, fixes pushed. CI status: {status}."
            )
        }
    };
    format!("[FIXES PUSHED] PR #{pr_number} - review comments addressed, fixes pushed.{suffix}")
}

fn commits_pushed_message(pr_number: u64, status: &str) -> String {
    let suffix = match status {
        "success" => " CI passing.",
        "pending" => " CI running.",
        "failure" => " CI failing.",
        _ => {
            return format!(
                "[COMMITS PUSHED] PR #{pr_number} - new commits pushed. CI status: {status}."
            )
        }
    };
    format!("[COMMITS PUSHED] PR #{pr_number} - new commits pushed.{suffix}")
}

fn stuck_message(pr_number: u64, rounds: u64) -> String {
    format!(
        "[STUCK: {pr_number}, rounds={rounds}] Review did not converge after {rounds} rounds. Dev leaf remains alive. Ask the human for clarification before continuing."
    )
}

fn native_event_action(
    event_type: &str,
    payload: &serde_json::Value,
    role: &str,
) -> Option<EventActionResponse> {
    match event_type {
        "pr_review" => native_pr_review_action(payload, role),
        "ci_status" => native_ci_status_action(payload, role),
        "sibling_merged" => Some(EventActionResponse::InjectMessage {
            message: sibling_merged_message(
                value_str(payload, "merged_branch")?,
                value_str(payload, "parent_branch")?,
            ),
        }),
        "issue_closed" => Some(EventActionResponse::InjectMessage {
            message: format!(
                "[ISSUE CLOSED: #{} closed by {}. Exiting; your worktree will be cleaned up.]",
                value_i64(payload, "issue_id")?,
                value_str(payload, "closed_by")?
            ),
        }),
        _ => None,
    }
}

fn native_pr_review_action(payload: &serde_json::Value, role: &str) -> Option<EventActionResponse> {
    if role == "tl" {
        return native_tl_pr_review_action(payload);
    }

    native_leaf_pr_review_action(payload)
}

fn native_tl_pr_review_action(payload: &serde_json::Value) -> Option<EventActionResponse> {
    let kind = value_str(payload, "kind")?;
    let pr_number = value_u64(payload, "pr_number")?;
    match kind {
        "review_received" | "reviewer_requested_changes" => {
            Some(EventActionResponse::InjectMessage {
                message: review_parent_message(
                    pr_number,
                    value_str(payload, "branch").unwrap_or("unknown"),
                    "changes requested",
                    value_str(payload, "author_branch"),
                    value_str(payload, "comments")?,
                ),
            })
        }
        "review_commented" => Some(EventActionResponse::InjectMessage {
            message: review_parent_message(
                pr_number,
                value_str(payload, "branch").unwrap_or("unknown"),
                "comment-only",
                value_str(payload, "author_branch"),
                value_str(payload, "comments")?,
            ),
        }),
        "approved" | "reviewer_approved" => Some(EventActionResponse::InjectMessage {
            message: pr_ready_message(pr_number),
        }),
        "timeout" => Some(EventActionResponse::InjectMessage {
            message: review_timeout_message(pr_number, value_u64(payload, "minutes")?),
        }),
        "fixes_pushed" => Some(EventActionResponse::InjectMessage {
            message: fixes_pushed_message(pr_number, value_str(payload, "ci_status")?),
        }),
        "commits_pushed" => Some(EventActionResponse::InjectMessage {
            message: commits_pushed_message(pr_number, value_str(payload, "ci_status")?),
        }),
        "rate_limited" => Some(EventActionResponse::InjectMessage {
            message: format!(
                "[RATE LIMITED] Review polling has {} retries remaining; reset in {} seconds.",
                value_u64(payload, "remaining")?,
                value_u64(payload, "reset_seconds")?
            ),
        }),
        "ci_triggered" => Some(EventActionResponse::InjectMessage {
            message: format!(
                "[CI TRIGGERED] PR #{pr_number} on {}.",
                value_str(payload, "branch")?
            ),
        }),
        "ci_blocked" => Some(EventActionResponse::InjectMessage {
            message: tl_ci_blocked_message(
                pr_number,
                value_str(payload, "ci_status")?,
                value_str(payload, "branch")?,
            ),
        }),
        "stuck" => Some(EventActionResponse::InjectMessage {
            message: stuck_message(pr_number, value_u64(payload, "rounds")?),
        }),
        "merge_ready" => Some(EventActionResponse::InjectMessage {
            message: merge_ready_message(
                pr_number,
                value_str(payload, "ci_status")?,
                value_str(payload, "branch")?,
            ),
        }),
        "dev_not_pushing" => Some(EventActionResponse::InjectMessage {
            message: format!("[DEV NOT PUSHING] PR #{pr_number} needs TL attention."),
        }),
        "reviewer_not_responding" => Some(EventActionResponse::InjectMessage {
            message: format!("[REVIEWER NOT RESPONDING] PR #{pr_number} needs TL attention."),
        }),
        "reviewer_never_started" => Some(EventActionResponse::InjectMessage {
            message: format!("[REVIEWER NEVER STARTED] PR #{pr_number} needs TL attention."),
        }),
        "dev_failed" => Some(EventActionResponse::InjectMessage {
            message: format!("[DEV FAILED] PR #{pr_number} needs TL attention."),
        }),
        _ => None,
    }
}

fn native_leaf_pr_review_action(payload: &serde_json::Value) -> Option<EventActionResponse> {
    let kind = value_str(payload, "kind")?;
    match kind {
        "review_received" | "reviewer_requested_changes" => {
            let pr_number = value_u64(payload, "pr_number")?;
            Some(EventActionResponse::NotifyParent {
                message: review_parent_message(
                    pr_number,
                    value_str(payload, "branch").unwrap_or("unknown"),
                    "changes requested",
                    value_str(payload, "author_branch"),
                    value_str(payload, "comments")?,
                ),
                pr_number: pr_number as i64,
            })
        }
        "review_commented" => {
            let pr_number = value_u64(payload, "pr_number")?;
            Some(EventActionResponse::NotifyParent {
                message: review_parent_message(
                    pr_number,
                    value_str(payload, "branch").unwrap_or("unknown"),
                    "comment-only",
                    value_str(payload, "author_branch"),
                    value_str(payload, "comments")?,
                ),
                pr_number: pr_number as i64,
            })
        }
        "ci_triggered" => Some(EventActionResponse::InjectMessage {
            message: format!(
                "[CI TRIGGERED] PR #{} on {}. Waiting for CI result.",
                value_u64(payload, "pr_number")?,
                value_str(payload, "branch")?
            ),
        }),
        "ci_blocked" => {
            let pr_number = value_u64(payload, "pr_number")?;
            Some(EventActionResponse::NotifyParent {
                message: ci_blocked_message(
                    pr_number,
                    value_str(payload, "ci_status")?,
                    value_str(payload, "branch")?,
                ),
                pr_number: pr_number as i64,
            })
        }
        "stuck" => Some(EventActionResponse::InjectMessage {
            message: format!(
                "Review loop stopped for PR #{} after {} rounds. Stay alive and wait for TL clarification.",
                value_u64(payload, "pr_number")?,
                value_u64(payload, "rounds")?
            ),
        }),
        "merge_ready" => {
            let pr_number = value_u64(payload, "pr_number")?;
            Some(EventActionResponse::NotifyParent {
                message: merge_ready_message(
                    pr_number,
                    value_str(payload, "ci_status")?,
                    value_str(payload, "branch")?,
                ),
                pr_number: pr_number as i64,
            })
        }
        "approved"
        | "reviewer_approved"
        | "timeout"
        | "fixes_pushed"
        | "commits_pushed"
        | "rate_limited"
        | "dev_not_pushing"
        | "reviewer_not_responding"
        | "reviewer_never_started"
        | "dev_failed" => Some(EventActionResponse::NoAction),
        _ => None,
    }
}

fn native_ci_status_action(payload: &serde_json::Value, role: &str) -> Option<EventActionResponse> {
    let pr_number = value_u64(payload, "pr_number")?;
    let status = value_str(payload, "status")?;
    let branch = value_str(payload, "branch")?;
    let merge_blocked_on_ci = payload
        .get("merge_blocked_on_ci")
        .and_then(|value| value.as_bool())
        .unwrap_or(false);
    let merge_ready = payload
        .get("merge_ready")
        .and_then(|value| value.as_bool())
        .unwrap_or(false);

    if role == "tl" {
        let message =
            if (merge_blocked_on_ci || merge_ready) && matches!(status, "success" | "neutral") {
                merge_ready_message(pr_number, status, branch)
            } else {
                ci_status_message(pr_number, status, branch)
            };
        return Some(EventActionResponse::InjectMessage { message });
    }

    if (merge_blocked_on_ci || merge_ready) && matches!(status, "success" | "neutral") {
        return Some(EventActionResponse::NotifyParent {
            message: merge_ready_message(pr_number, status, branch),
            pr_number: pr_number as i64,
        });
    }

    if merge_blocked_on_ci && status == "failure" {
        return Some(EventActionResponse::NotifyParent {
            message: ci_blocked_message(pr_number, status, branch),
            pr_number: pr_number as i64,
        });
    }

    Some(EventActionResponse::InjectMessage {
        message: ci_status_message(pr_number, status, branch),
    })
}

/// Observation collected from Forgejo and worktree state for one open PR.
struct Observation {
    head_sha: String,
    review_state: ForgejoReviewState,
    comments: Vec<ForgejoReviewComment>,
    reviews: Vec<ForgejoReview>,
    changes_requested_rounds: u32,
    ci_status: CIStatus,
    forgejo_review_present: bool,
}

/// Replaces `github_poller.rs` and `copilot_review.rs` by observing Forgejo
/// PR/review/CI state and git worktree state.
pub struct WorktreeEventWatcher<C> {
    ctx: Arc<C>,
    poll_interval: Duration,
    inbox_poke_interval: Duration,
    state: Arc<WatcherRuntimeState>,
    watcher_state_path: std::path::PathBuf,
    plugins: Option<PluginMap>,
    policy: ReviewPolicy,
    /// Shared CI status map updated by Forgejo webhook fast-path notifications.
    ci_status_map: Arc<RwLock<CiStatusMap>>,
    ci_source_configured: bool,
    /// Spawns reviewer agents on PR creation.
    reviewer_spawner: Option<Arc<dyn ReviewerSpawner>>,
    forgejo_absent_warned: Arc<AtomicBool>,
}

impl<C> WorktreeEventWatcher<C>
where
    C: HasTeamRegistry
        + HasAgentResolver
        + HasEventLog
        + HasEventQueue
        + HasForgejoClient
        + HasGitWorktreeService
        + HasInboxStore
        + HasProjectDir
        + 'static,
{
    pub fn new(ctx: Arc<C>) -> Self {
        let watcher_state_path = ctx.project_dir().join(".exo/watcher-state.json");
        Self {
            ctx,
            poll_interval: Duration::from_secs(60),
            inbox_poke_interval: DEFAULT_INBOX_POKE_INTERVAL,
            state: Arc::new(WatcherRuntimeState::new()),
            watcher_state_path,
            plugins: None,
            policy: ReviewPolicy::default(),
            ci_status_map: Arc::new(RwLock::new(HashMap::new())),
            ci_source_configured: false,
            reviewer_spawner: None,
            forgejo_absent_warned: Arc::new(AtomicBool::new(false)),
        }
    }

    pub fn with_poll_interval(mut self, interval: Duration) -> Self {
        self.poll_interval = interval;
        self
    }

    pub fn with_inbox_poke_interval(mut self, interval: Duration) -> Self {
        self.inbox_poke_interval = interval;
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

    pub fn with_runtime_state(mut self, state: Arc<WatcherRuntimeState>) -> Self {
        self.state = state;
        self
    }

    /// Use a shared CI status map (e.g. from `Services`) instead of the internal one.
    ///
    /// Call this so the merge handler and the watcher read from the same map.
    pub fn with_ci_status_map(mut self, map: Arc<RwLock<CiStatusMap>>) -> Self {
        self.ci_status_map = map;
        self
    }

    pub fn with_ci_source_configured(mut self, configured: bool) -> Self {
        self.ci_source_configured = configured;
        self
    }

    fn ci_source_configured(&self) -> bool {
        self.ci_source_configured || self.ctx.forgejo_client().is_some()
    }

    fn spawn_reviewer_for_pr(&self, pr_number: u64, pr: &PrEntry, head_sha: &str, reason: &str) {
        let Some(spawner) = &self.reviewer_spawner else {
            warn!(
                pr_number,
                head_sha,
                reason,
                "Reviewer spawner is not configured; skipping reviewer auto-spawn"
            );
            return;
        };

        let spawner = spawner.clone();
        let pr_clone = pr.clone();
        let state = self.state.clone();
        let head_sha = head_sha.to_string();
        let reason = reason.to_string();
        tokio::spawn(async move {
            info!(pr_number, head_sha = %head_sha, reason = %reason, "Spawning reviewer agent for PR");
            match spawner.spawn_reviewer_for_pr(&pr_clone).await {
                Ok(_) => {
                    info!(
                        pr_number,
                        head_sha = %head_sha,
                        reason = %reason,
                        "Reviewer agent spawned successfully for PR"
                    );
                    if let Some(ws) = state.prs.lock().await.get_mut(&pr_number) {
                        ws.reviewer_spawned = true;
                    }
                }
                Err(e) => {
                    warn!(
                        pr_number,
                        head_sha = %head_sha,
                        reason = %reason,
                        error = %e,
                        "Failed to spawn reviewer for PR"
                    )
                }
            }
        });
    }

    async fn observed_ci_status(&self, branch: &BranchName, head_sha: &str) -> CIStatus {
        if !self.policy.ci.gate.enabled(self.ci_source_configured()) {
            return CIStatus::Neutral;
        }

        let Some(forgejo) = self.ctx.forgejo_client() else {
            return CIStatus::Unknown;
        };
        let Ok(repo_info) = repo::get_repo_info(self.ctx.project_dir()).await else {
            return CIStatus::Unknown;
        };
        match forgejo
            .commit_status_for_head(&repo_info.owner, &repo_info.repo, head_sha)
            .await
        {
            Ok(status) => status,
            Err(error) => {
                debug!(branch = %branch, head_sha, error = %error, "Forgejo commit status lookup failed");
                CIStatus::Unknown
            }
        }
    }
    pub async fn run(&self) {
        tracing::info!(
            poll_interval_secs = self.poll_interval.as_secs(),
            "Forgejo worktree event watcher started"
        );

        self.append_watcher_log("watcher started").await;

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

    async fn append_watcher_log(&self, message: &str) {
        let log_path = self.ctx.project_dir().join(".exo/logs/watcher.log");
        let timestamp = Utc::now().format("%Y-%m-%dT%H:%M:%SZ");
        let line = format!("{} [watcher] {}\n", timestamp, message);
        if let Some(parent) = log_path.parent() {
            let _ = tokio::fs::create_dir_all(parent).await;
        }
        use tokio::io::AsyncWriteExt;
        if let Ok(mut file) = tokio::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .await
        {
            let _ = file.write_all(line.as_bytes()).await;
        }
    }

    async fn read_watcher_state(&self) -> Result<WatcherStateFile> {
        if !self.watcher_state_path.exists() {
            return Ok(WatcherStateFile::default());
        }
        let data = tokio::fs::read_to_string(&self.watcher_state_path)
            .await
            .with_context(|| format!("failed to read {}", self.watcher_state_path.display()))?;
        serde_json::from_str(&data).context("failed to parse watcher-state.json")
    }

    async fn write_watcher_state(&self, state: &WatcherStateFile) -> Result<()> {
        if let Some(parent) = self.watcher_state_path.parent() {
            tokio::fs::create_dir_all(parent).await?;
        }
        let data = serde_json::to_string_pretty(state)?;
        tokio::fs::write(&self.watcher_state_path, data).await?;
        Ok(())
    }

    async fn evict_closed_prs_from_watcher_state(&self, registry: &PrRegistry) -> Result<()> {
        let mut state = self.read_watcher_state().await.unwrap_or_default();
        let evicted = evict_closed_prs_from_state(&mut state, registry);
        if evicted.is_empty() {
            return Ok(());
        }

        self.write_watcher_state(&state).await?;
        info!(prs = ?evicted, "Evicted closed PRs from watcher state");
        self.append_watcher_log(&format!(
            "evicted closed PR state: {}",
            evicted
                .iter()
                .map(u64::to_string)
                .collect::<Vec<_>>()
                .join(",")
        ))
        .await;
        Ok(())
    }

    async fn load_registry_from_forgejo(&self) -> Result<PrRegistry> {
        let Some(forgejo) = self.ctx.forgejo_client() else {
            if !self.forgejo_absent_warned.swap(true, Ordering::Relaxed) {
                tracing::warn!(
                    "[Watcher] Forgejo client not configured - watcher idle. Set forgejo_url and forgejo_token in .exo/config.toml"
                );
            }
            return Ok(PrRegistry::default());
        };
        let repo_info = repo::get_repo_info(self.ctx.project_dir()).await?;
        let watcher_state = self.read_watcher_state().await.unwrap_or_default();
        let pull_requests = forgejo
            .list_open_pull_requests(&repo_info.owner, &repo_info.repo)
            .await?;
        let mut registry = PrRegistry::default();

        for pr in pull_requests {
            let metadata = parse_pr_body_metadata(&pr.body);
            let number = pr.number.as_u64();
            let persisted = watcher_state.prs.get(&number).cloned().unwrap_or_default();
            let birth_branch = metadata
                .birth_branch
                .as_deref()
                .unwrap_or(pr.head_ref.as_str());
            let author_agent = metadata
                .author_agent
                .or_else(|| author_agent_from_branch(birth_branch))
                .unwrap_or_else(|| pr.head_ref.to_string());
            let author_role = metadata.author_role.unwrap_or_else(|| "dev".to_string());
            let head_sha = pr.head_sha.clone();
            registry.prs.insert(
                number,
                PrEntry {
                    number,
                    head_branch: pr.head_ref.to_string(),
                    base_branch: pr.base_ref.to_string(),
                    title: pr.title,
                    body: pr.body,
                    author_agent,
                    author_role,
                    created_at: Utc::now(),
                    state: PrState::Open,
                    last_review_at: None,
                    last_head_sha: head_sha,
                    approved_at_sha: None,
                    reviewer_agent: metadata.reviewer_agent,
                    reviewer_birth_branch: metadata.reviewer_birth_branch,
                    rounds: persisted.rounds,
                    stuck: persisted.stuck,
                    needs_human_review: persisted.needs_human_review,
                    merge_blocked_on_ci: false,
                    chainlink_issue_id: metadata.chainlink_issue_id,
                },
            );
        }

        Ok(registry)
    }

    async fn set_pr_stuck(&self, pr_number: u64, rounds: u32) -> anyhow::Result<()> {
        let mut state = self.read_watcher_state().await.unwrap_or_default();
        let entry = state.prs.entry(pr_number).or_default();
        entry.stuck = true;
        entry.rounds = rounds;
        entry.needs_human_review = true;
        self.write_watcher_state(&state).await?;
        info!(pr_number, rounds, "Set stuck flag in watcher state");
        Ok(())
    }

    async fn set_pr_rounds(&self, pr_number: u64, rounds: u32) -> anyhow::Result<()> {
        let mut state = self.read_watcher_state().await.unwrap_or_default();
        let entry = state.prs.entry(pr_number).or_default();
        if entry.rounds != rounds {
            entry.rounds = rounds;
            self.write_watcher_state(&state).await?;
            info!(
                pr_number,
                rounds, "Persisted PR review rounds in watcher state"
            );
        }
        Ok(())
    }

    #[instrument(skip_all, name = "worktree_event_watcher.poll_cycle")]
    async fn poll_cycle(&self) -> Result<()> {
        let registry = self.load_registry_from_forgejo().await?;
        let pr_count = registry.prs.len();
        tracing::info!(pr_count, "[Watcher] poll cycle");
        self.append_watcher_log(&format!("poll: {} open PR(s)", pr_count))
            .await;
        self.evict_closed_prs_from_watcher_state(&registry).await?;
        if !registry.prs.is_empty() {
            let observations = self.collect_observations(&registry).await?;
            for (num, obs) in &observations {
                tracing::info!(
                    pr = num,
                    review_state = ?obs.review_state,
                    ci_status = ?obs.ci_status,
                    head_sha = %obs.head_sha,
                    changes_requested_rounds = obs.changes_requested_rounds,
                    "[Watcher] PR observation"
                );
            }
            self.append_watcher_log(&format!(
                "observations: {}",
                format_observations(&observations)
            ))
            .await;
            let removed = self.process_observations(&registry, &observations).await?;
            self.detect_merged(&registry, &removed).await?;
        }

        self.poke_unread_inbox_agents().await?;
        Ok(())
    }

    async fn poke_unread_inbox_agents(&self) -> Result<()> {
        let candidates = self
            .ctx
            .inbox_store()
            .agents_needing_poke(self.inbox_poke_interval.as_secs())
            .context("failed to query inbox poke candidates")?;
        if candidates.is_empty() {
            return Ok(());
        }

        let from =
            AgentName::try_from_str("watcher").expect("literal validated string is non-empty");
        for candidate in candidates {
            let Ok(agent_name) = AgentName::try_from_str(candidate.agent_id.as_str()) else {
                warn!(agent = %candidate.agent_id, "Skipping inbox poke for invalid agent id");
                continue;
            };
            let message = inbox_poke_message(candidate.unread_count);
            let outcome = crate::services::delivery::route_tmux_notification(
                &*self.ctx,
                &crate::domain::Address::Agent(agent_name),
                &from,
                &message,
                "Unread inbox poke",
            )
            .await;
            if outcome.is_success() {
                self.ctx
                    .inbox_store()
                    .record_poke(
                        candidate.agent_id.as_str(),
                        candidate.newest_message_id,
                        self.inbox_poke_interval.as_secs(),
                        MAX_INBOX_POKE_INTERVAL.as_secs(),
                    )
                    .context("failed to record inbox poke metadata")?;
                info!(
                    agent = %candidate.agent_id,
                    unread_count = candidate.unread_count,
                    "Poked idle agent with unread inbox mail"
                );
            } else {
                warn!(
                    agent = %candidate.agent_id,
                    unread_count = candidate.unread_count,
                    method = %outcome.method_string(),
                    "Failed to poke idle agent with unread inbox mail"
                );
            }
        }
        Ok(())
    }

    async fn collect_observations(
        &self,
        registry: &crate::services::pr_registry::PrRegistry,
    ) -> Result<HashMap<u64, Observation>> {
        let mut observations = HashMap::new();
        let project_dir = self.ctx.project_dir().to_path_buf();

        for (number, pr) in &registry.prs {
            if pr.state != PrState::Open {
                continue;
            }

            let head_sha = match pr.last_head_sha.as_deref() {
                Some(sha) if !sha.is_empty() => sha.to_string(),
                _ => {
                    let worktree_path = project_dir.join(".exo/worktrees").join(&pr.author_agent);
                    git_head_sha(&worktree_path).await.unwrap_or_default()
                }
            };

            let (review_state, comments, reviews, changes_requested_rounds, forgejo_review_present) =
                self.forgejo_review_parts(*number, &head_sha).await;

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
                    changes_requested_rounds,
                    ci_status,
                    forgejo_review_present,
                },
            );
        }

        Ok(observations)
    }

    async fn process_observations(
        &self,
        registry: &crate::services::pr_registry::PrRegistry,
        observations: &HashMap<u64, Observation>,
    ) -> Result<Vec<u64>> {
        let mut removed_prs = Vec::new();
        let mut pending_actions: Vec<PendingPrActions> = Vec::new();
        let mut reviewer_disposals: Vec<u64> = Vec::new();
        let mut head_sha_updates: Vec<(u64, String)> = Vec::new();
        let watcher_state = self.read_watcher_state().await.unwrap_or_default();

        {
            let mut state_guard = self.state.prs.lock().await;

            for (pr_number, obs) in observations {
                let pr = match registry.prs.get(pr_number) {
                    Some(p) => p,
                    None => continue,
                };

                let agent_name = &pr.author_agent;
                let (branch, agent_type, agent_role) = review_event_target(pr);
                let persisted = watcher_state
                    .prs
                    .get(pr_number)
                    .cloned()
                    .unwrap_or_default();
                let persisted_last_head_sha = persisted.last_head_sha.as_deref();
                let persisted_review_fingerprint = persisted.last_review_fingerprint.clone();
                let runtime_last_head_sha = state_guard
                    .get(pr_number)
                    .map(|state| state.last_sha.as_str());
                let last_observed_head_sha = persisted_last_head_sha
                    .or(runtime_last_head_sha)
                    .or(pr.last_head_sha.as_deref());
                let head_sha_changed = last_observed_head_sha
                    .is_some_and(|last_head_sha| last_head_sha != obs.head_sha.as_str());
                let stale_terminal_review_after_head_change = head_sha_changed
                    && review_state_disposes_reviewer(&obs.review_state)
                    && !obs.forgejo_review_present;
                let terminal_review_observed = review_state_disposes_reviewer(&obs.review_state)
                    && !stale_terminal_review_after_head_change;
                let (local_reviews, _local_review_state) =
                    if stale_terminal_review_after_head_change {
                        (Vec::new(), ForgejoReviewVerdict::None)
                    } else {
                        obs_to_review_parts(obs)
                    };
                head_sha_updates.push((*pr_number, obs.head_sha.clone()));
                let actions = if let Some(old_state) = state_guard.get_mut(pr_number) {
                    if head_sha_changed {
                        old_state.reviewer_spawned = false;
                        old_state.reviewer_disposed = false;
                        old_state.stuck = false;
                    }
                    if should_spawn_reviewer_for_new_head(
                        old_state,
                        self.policy.reviewer_max_rounds,
                    ) {
                        let reason = if head_sha_changed {
                            "new_head_sha"
                        } else {
                            "retry_missing_reviewer"
                        };
                        self.spawn_reviewer_for_pr(*pr_number, pr, &obs.head_sha, reason);
                    }
                    compute_pr_actions_with_context(
                        old_state,
                        PRNumber::new(*pr_number),
                        &obs.head_sha,
                        &obs.comments,
                        &local_reviews,
                        obs.changes_requested_rounds,
                        obs.ci_status,
                        pr.merge_blocked_on_ci,
                        pr.reviewer_agent.is_some(),
                        obs.forgejo_review_present,
                        branch.as_str(),
                        &|c, r| format_review_message(c, r),
                        self.policy.reviewer_max_rounds,
                        self.policy.reviewer_max_wait_seconds,
                    )
                } else {
                    let last_sha = persisted_last_head_sha.unwrap_or(&obs.head_sha);
                    let mut new_state =
                        WatchState::new(&branch, agent_type, last_sha, CIStatus::Unknown, 0);
                    new_state.last_review_state = persisted.last_review_state.clone();
                    new_state.last_review_fingerprint = persisted_review_fingerprint;
                    new_state.notified_parent_timeout = persisted.notified_parent_timeout;
                    new_state.notified_parent_approved = persisted.notified_parent_approved;
                    new_state.addressed_changes = persisted.addressed_changes;
                    new_state.rounds = persisted.rounds;
                    new_state.stuck = persisted.stuck || persisted.needs_human_review;
                    new_state.merge_ready_notified = persisted.merge_ready_notified;
                    new_state.ci_triggered_sha = persisted.ci_triggered_sha.clone();
                    new_state.ci_blocked_notified = persisted.ci_blocked_notified;
                    state_guard.insert(*pr_number, new_state);
                    let actions = compute_pr_actions_with_context(
                        state_guard
                            .get_mut(pr_number)
                            .expect("watch state inserted above"),
                        PRNumber::new(*pr_number),
                        &obs.head_sha,
                        &obs.comments,
                        &local_reviews,
                        obs.changes_requested_rounds,
                        obs.ci_status,
                        pr.merge_blocked_on_ci,
                        pr.reviewer_agent.is_some(),
                        obs.forgejo_review_present,
                        branch.as_str(),
                        &|c, r| format_review_message(c, r),
                        self.policy.reviewer_max_rounds,
                        self.policy.reviewer_max_wait_seconds,
                    );
                    // Spawn reviewer immediately on first sighting of a new open PR
                    // unless the watcher restarted after a terminal review verdict.
                    let should_spawn_reviewer = state_guard.get(pr_number).is_some_and(|state| {
                        should_spawn_reviewer_for_new_head(state, self.policy.reviewer_max_rounds)
                    });
                    if !terminal_review_observed && should_spawn_reviewer {
                        self.spawn_reviewer_for_pr(*pr_number, pr, &obs.head_sha, "first_sighting");
                    }
                    actions
                };

                if terminal_review_observed && !head_sha_changed {
                    if let Some(ws) = state_guard.get_mut(pr_number) {
                        if !ws.reviewer_disposed {
                            tracing::info!(
                                pr_number = *pr_number,
                                head_sha_changed,
                                "disposing reviewer after terminal review observed"
                            );
                            reviewer_disposals.push(*pr_number);
                            ws.reviewer_disposed = true;
                        }
                    }
                }

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

        self.persist_last_head_shas(&head_sha_updates).await?;
        for pr_number in reviewer_disposals {
            let reviewer_slugs = dispose_reviewers_for_pr(
                self.ctx.project_dir(),
                self.ctx.git_worktree_service().clone(),
                pr_number,
            )
            .await;
            let log_line = reviewer_disposal_log_line(pr_number, &reviewer_slugs);
            if reviewer_slugs.is_empty() {
                warn!(pr_number, "{log_line}");
            } else {
                info!(pr_number, reviewer_slugs = ?reviewer_slugs, "terminal review triggered reviewer disposal");
            }
            self.append_watcher_log(&log_line).await;
        }

        for pending in pending_actions {
            for action in pending.actions {
                match action {
                    PendingAction::WasmEvent {
                        event_type,
                        payload,
                    } => {
                        let release_message = merge_ready_release_message(&payload);

                        let payload_kind = payload
                            .get("kind")
                            .and_then(|v| v.as_str())
                            .unwrap_or("<unknown>")
                            .to_string();
                        info!(
                            pr_number = pending.pr_number,
                            agent_name = %pending.agent_name,
                            branch = %pending.branch,
                            event_type,
                            kind = %payload_kind,
                            "Dispatching WasmEvent"
                        );
                        let requests_merge_ready_delivery =
                            requests_merge_ready_parent_delivery(event_type, &payload);
                        let is_review_feedback = is_review_feedback_event(event_type, &payload);

                        // Reviewers are ephemeral: they submit their Forgejo verdict and exit.
                        // Review events have one delivery target, the PR owner's dev leaf. A
                        // subsequent review round is started by the watcher-owned
                        // spawn_reviewer_for_pr state machine above; never inject an event into
                        // the already-exited reviewer branch.

                        let response = self
                            .call_handle_event_for_role(
                                pending.branch.as_str(),
                                pending.agent_type,
                                &pending.agent_role,
                                event_type,
                                payload,
                            )
                            .await;
                        let delivery_confirmed = match response {
                            Ok(Some(response)) => {
                                let confirmed = self
                                    .handle_event_action(
                                        response,
                                        pending.branch.as_str(),
                                        pending.agent_type,
                                    )
                                    .await;
                                if confirmed && requests_merge_ready_delivery {
                                    self.mark_merge_ready_notified(pending.pr_number).await;
                                }
                                if confirmed {
                                    if let Some(message) = release_message {
                                        self.deliver_release_message(
                                            pending.branch.as_str(),
                                            pending.agent_type,
                                            &message,
                                        )
                                        .await;
                                    }
                                }
                                confirmed
                            }
                            Ok(None) => {
                                warn!(
                                    pr_number = pending.pr_number,
                                    event_type, "Event handler returned no action"
                                );
                                false
                            }
                            Err(error) => {
                                warn!(
                                    pr_number = pending.pr_number,
                                    event_type,
                                    %error,
                                    "Event handler dispatch failed"
                                );
                                false
                            }
                        };
                        if is_review_feedback && !delivery_confirmed {
                            if let Err(error) = self
                                .reset_review_delivery_fingerprint(pending.pr_number)
                                .await
                            {
                                warn!(
                                    pr_number = pending.pr_number,
                                    %error,
                                    "Failed to reset undelivered review fingerprint"
                                );
                            }
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
                        info!(
                            pr_number,
                            classification = classification.as_str(),
                            branch = %diagnostic.branch,
                            head_sha = %diagnostic.head_sha,
                            last_observed_sha = %diagnostic.last_observed_sha,
                            rounds = diagnostic.rounds,
                            reviewer_registered = diagnostic.reviewer_registered,
                            forgejo_review_present = diagnostic.forgejo_review_present,
                            wait_seconds = diagnostic.wait_seconds,
                            ci_status = %diagnostic.ci_status,
                            "Review-loop human handoff required; watcher does not create Chainlink issues"
                        );
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
        if updates.is_empty() {
            return Ok(());
        }

        let mut state = self.read_watcher_state().await.unwrap_or_default();
        let runtime_state = self.state.prs.lock().await;
        for (pr_number, head_sha) in updates {
            let entry = state.prs.entry(*pr_number).or_default();
            entry.last_head_sha = Some(head_sha.clone());
            if let Some(watch_state) = runtime_state.get(pr_number) {
                entry.rounds = watch_state.rounds;
                entry.stuck = watch_state.stuck;
                entry.needs_human_review = watch_state.stuck;
                entry.last_review_state = watch_state.last_review_state.clone();
                entry.last_review_fingerprint = watch_state.last_review_fingerprint.clone();
                entry.notified_parent_timeout = watch_state.notified_parent_timeout;
                entry.notified_parent_approved = watch_state.notified_parent_approved;
                entry.addressed_changes = watch_state.addressed_changes;
                entry.merge_ready_notified = watch_state.merge_ready_notified;
                entry.ci_triggered_sha = watch_state.ci_triggered_sha.clone();
                entry.ci_blocked_notified = watch_state.ci_blocked_notified;
            }
        }
        drop(runtime_state);
        self.write_watcher_state(&state).await?;
        debug!(
            count = updates.len(),
            "Persisted last observed PR head SHAs"
        );
        Ok(())
    }

    async fn reset_review_delivery_fingerprint(&self, pr_number: u64) -> Result<()> {
        let mut runtime_state = self.state.prs.lock().await;
        if let Some(watch_state) = runtime_state.get_mut(&pr_number) {
            watch_state.last_review_fingerprint = None;
        }
        drop(runtime_state);

        let mut state = self.read_watcher_state().await.unwrap_or_default();
        let changed = state
            .prs
            .get_mut(&pr_number)
            .and_then(|pr_state| pr_state.last_review_fingerprint.take())
            .is_some();
        if changed {
            self.write_watcher_state(&state).await?;
        }
        Ok(())
    }

    async fn detect_merged(
        &self,
        registry: &crate::services::pr_registry::PrRegistry,
        removed: &[u64],
    ) -> Result<()> {
        if removed.is_empty() {
            return Ok(());
        }

        let state_guard = self.state.prs.lock().await;

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

        let event_input = serde_json::json!({
            "role": role,
            "event_type": event_type,
            "payload": payload,
        });

        let plugins_guard = plugins.read().await;
        let plugin = match plugins_guard.get(&agent_name) {
            Some(p) => p.clone(),
            None => {
                if let Some(action) = native_event_action(event_type, &payload, role) {
                    tracing::info!(
                        branch,
                        lookup_key = %agent_name,
                        ?agent_type,
                        role,
                        event_type,
                        "No WASM plugin for event target; using native Rust-side delivery"
                    );
                    return Ok(Some(action));
                }
                log_missing_event_plugin(branch, &agent_name, agent_type, role, event_type);
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

    async fn mark_merge_ready_notified(&self, pr_number: u64) {
        let mut state_guard = self.state.prs.lock().await;
        let marked = if let Some(state) = state_guard.get_mut(&pr_number) {
            state.merge_ready_notified = true;
            true
        } else {
            warn!(
                pr_number,
                "Cannot mark merge-ready notification delivered because watcher state is missing"
            );
            false
        };
        drop(state_guard);

        if marked {
            let mut persisted = self.read_watcher_state().await.unwrap_or_default();
            persisted
                .prs
                .entry(pr_number)
                .or_default()
                .merge_ready_notified = true;
            if let Err(error) = self.write_watcher_state(&persisted).await {
                warn!(
                    pr_number,
                    %error,
                    "Failed to persist merge-ready notification state"
                );
            }
        }
    }

    async fn handle_event_action(
        &self,
        action: EventActionResponse,
        branch: &str,
        agent_type: AgentType,
    ) -> bool {
        match action {
            EventActionResponse::InjectMessage { message } => {
                let preview: String = message.chars().take(200).collect();
                info!(
                    branch,
                    message = %preview,
                    "handle_event_action: InjectMessage"
                );
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
                !matches!(
                    crate::services::delivery::deliver_to_agent(
                        &*self.ctx,
                        branch,
                        &tab_name,
                        &AgentName::try_from_str("event-handler")
                            .expect("literal validated string is non-empty"),
                        &message,
                        "Event handler action",
                    )
                    .await,
                    crate::services::delivery::DeliveryResult::Failed
                )
            }
            EventActionResponse::NotifyParent {
                message,
                pr_number: _pr_number,
            } => {
                let agent_slug = branch.rsplit_once('.').map(|(_, s)| s).unwrap_or(branch);
                let parent_session_id = match branch.rsplit_once('.') {
                    Some((parent, _)) => {
                        crate::services::delivery::canonical_parent_recipient(parent)
                    }
                    None => "root".to_string(),
                };
                let preview: String = message.chars().take(200).collect();
                info!(
                    agent_slug,
                    parent_session_id = %parent_session_id,
                    message = %preview,
                    "handle_event_action: NotifyParent"
                );
                let parent_name = AgentName::try_from_str(parent_session_id.as_str())
                    .expect("validated string input is non-empty");
                let parent_tab = crate::services::delivery::resolve_tab_name_for_agent(
                    &parent_name,
                    Some(self.ctx.agent_resolver()),
                );

                let agent_name = AgentName::try_from_str(agent_slug)
                    .expect("validated string input is non-empty");
                !matches!(
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
                    .await,
                    crate::services::delivery::DeliveryResult::Failed
                )
            }
            EventActionResponse::NoAction => false,
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
        comments: Option<Vec<ForgejoReviewComment>>,
        reviews: Option<Vec<ForgejoReview>>,
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

    async fn forgejo_review_parts(
        &self,
        pr_number: u64,
        head_sha: &str,
    ) -> (
        ForgejoReviewState,
        Vec<ForgejoReviewComment>,
        Vec<ForgejoReview>,
        u32,
        bool,
    ) {
        let Some(forgejo) = self.ctx.forgejo_client() else {
            warn!(
                pr_number,
                "forgejo_review_parts: no Forgejo client configured"
            );
            return (ForgejoReviewState::PendingReview, vec![], vec![], 0, false);
        };
        let repo_info = match repo::get_repo_info(self.ctx.project_dir()).await {
            Ok(info) => info,
            Err(e) => {
                warn!(pr_number, error = %e, "forgejo_review_parts: get_repo_info failed");
                return (ForgejoReviewState::PendingReview, vec![], vec![], 0, false);
            }
        };
        let reviews = match forgejo
            .list_pull_request_reviews(&repo_info.owner, &repo_info.repo, PRNumber::new(pr_number))
            .await
        {
            Ok(reviews) => reviews,
            Err(error) => {
                debug!(pr_number, error = %error, "Forgejo review lookup failed");
                return (ForgejoReviewState::PendingReview, vec![], vec![], 0, false);
            }
        };

        let mut local_reviews = Vec::new();
        let mut all_reviews = Vec::new();
        for review in reviews {
            let state = review_state_from_str(&review.state);
            if state == ForgejoReviewVerdict::None {
                continue;
            }
            let local_review = ForgejoReview {
                review_id: review.id,
                body: review.body,
                state,
                author_branch: None,
                commit_id: review.commit_id,
            };
            all_reviews.push(local_review.clone());

            if let Some(review_commit) = local_review
                .commit_id
                .as_deref()
                .filter(|commit| !head_sha.is_empty() && *commit != head_sha)
            {
                self.append_watcher_log(&dropped_review_by_sha_log_line(
                    pr_number,
                    review_commit,
                    head_sha,
                ))
                .await;
                continue;
            }
            local_reviews.push(local_review);
        }

        let review_state = aggregate_review_state(&local_reviews);

        let changes_requested_rounds = distinct_changes_requested_rounds(&all_reviews);
        let forgejo_review_present = !local_reviews.is_empty();

        let mut inline_comments: Vec<ForgejoReviewComment> = Vec::new();
        for review in &local_reviews {
            let Some(review_id) = review.review_id else {
                continue;
            };
            let comments = match forgejo
                .list_pull_request_review_comments(
                    &repo_info.owner,
                    &repo_info.repo,
                    PRNumber::new(pr_number),
                    review_id,
                )
                .await
            {
                Ok(comments) => comments,
                Err(error) => {
                    debug!(pr_number, review_id, error = %error, "Forgejo review comment fetch failed");
                    continue;
                }
            };
            for comment in comments {
                inline_comments.push(ForgejoReviewComment {
                    body: comment.body,
                    path: comment.path,
                    diff_hunk: comment.diff_hunk,
                    thread_id: comment.in_reply_to.map(|id| id.to_string()),
                    resolved: false,
                    author_branch: None,
                });
            }
        }

        (
            review_state,
            inline_comments,
            local_reviews,
            changes_requested_rounds,
            forgejo_review_present,
        )
    }
}

/// Pure state machine: given old state + new observations, compute pending actions.
#[allow(clippy::too_many_arguments)]
#[cfg(test)]
fn compute_pr_actions(
    old_state: &mut WatchState,
    pr_number: PRNumber,
    pr_sha: &str,
    comments: &[ForgejoReviewComment],
    reviews: &[ForgejoReview],
    ci_status: CIStatus,
    merge_blocked_on_ci: bool,
    branch: &str,
    format_message: &dyn Fn(&[ForgejoReviewComment], &[ForgejoReview]) -> String,
    max_rounds: u32,
) -> Vec<PendingAction> {
    compute_pr_actions_with_context(
        old_state,
        pr_number,
        pr_sha,
        comments,
        reviews,
        distinct_changes_requested_rounds(reviews),
        ci_status,
        merge_blocked_on_ci,
        false,
        false,
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
    comments: &[ForgejoReviewComment],
    reviews: &[ForgejoReview],
    observed_request_change_rounds: u32,
    ci_status: CIStatus,
    merge_blocked_on_ci: bool,
    reviewer_registered: bool,
    forgejo_review_present: bool,
    branch: &str,
    format_message: &dyn Fn(&[ForgejoReviewComment], &[ForgejoReview]) -> String,
    max_rounds: u32,
    max_wait_seconds: u64,
) -> Vec<PendingAction> {
    let mut pending_actions = Vec::new();
    let mut emitted_merge_ready_notification = false;
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
        let was_changes_requested =
            old_state.last_review_state == ForgejoReviewVerdict::ChangesRequested;
        old_state.last_sha = pr_sha.to_string();
        old_state.last_review_state = ForgejoReviewVerdict::None;
        old_state.notified_parent_approved = false;
        old_state.notified_parent_timeout = false;
        old_state.review_approved_at = None;
        old_state.merge_ready_notified = false;
        old_state.ci_triggered_sha = None;
        old_state.ci_blocked_notified = false;
        old_state.stuck = false;
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
                forgejo_review_present,
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

    let terminal_parent_notified =
        old_state.merge_ready_notified || old_state.notified_parent_timeout || old_state.stuck;
    if terminal_parent_notified && !recover_after_ci_block && !merge_ready_now {
        return pending_actions;
    }

    if comment_count != old_state.pr_review_cycle_count {
        old_state.pr_review_cycle_count = comment_count;
    }

    let observed_request_change_rounds =
        observed_request_change_rounds.max(distinct_changes_requested_rounds(reviews));
    let next_review_round = observed_request_change_rounds.max(old_state.rounds + 1);

    let approved = reviews.iter().any(|r| {
        r.state == ForgejoReviewVerdict::Approved || r.body.to_lowercase().contains("approved")
    });
    if approved && old_state.last_review_state != ForgejoReviewVerdict::Approved {
        let approved_round =
            approved_review_round(old_state.rounds, observed_request_change_rounds);
        old_state.rounds = approved_round;
        pending_actions.push(PendingAction::WriteRegistryRounds {
            pr_number: pr_number.as_u64(),
            rounds: old_state.rounds,
        });
        old_state.last_review_state = ForgejoReviewVerdict::Approved;
        old_state.notified_parent_approved = true;
        old_state.review_approved_at = Some(now);
        let merge_ready_now = !old_state.merge_ready_notified
            && signals_within_merge_ready_window(
                old_state.review_approved_at,
                old_state.ci_mergeable_at,
            );
        pending_actions.push(PendingAction::WasmEvent {
            event_type: "pr_review",
            payload: serde_json::json!({
                "kind": review_event_kind_for_state(&ForgejoReviewVerdict::Approved)
                    .expect("approved review state has an event kind"),
                "pr_number": pr_number.as_u64(),
                "ci_status": ci_status.as_str(),
                "branch": branch,
            }),
        });
        if merge_ready_now {
            emitted_merge_ready_notification = true;
            pending_actions.push(PendingAction::WasmEvent {
                event_type: "pr_review",
                payload: serde_json::json!({
                    "kind": "merge_ready",
                    "pr_number": pr_number.as_u64(),
                    "ci_status": ci_status.as_str(),
                    "branch": branch,
                }),
            });
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
    }

    let changes_requested = reviews
        .iter()
        .any(|r| r.state == ForgejoReviewVerdict::ChangesRequested);
    let changes_requested_fingerprint =
        review_fingerprint(comments, reviews, ForgejoReviewVerdict::ChangesRequested);
    if !approved
        && changes_requested
        && (old_state.last_review_state != ForgejoReviewVerdict::ChangesRequested
            || old_state.last_review_fingerprint != changes_requested_fingerprint)
    {
        old_state.last_review_state = ForgejoReviewVerdict::ChangesRequested;
        old_state.last_review_fingerprint = changes_requested_fingerprint;
        old_state.first_seen = now;
        old_state.rounds = next_review_round;

        let message = format_message(comments, reviews);
        if old_state.rounds >= max_rounds {
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
                    forgejo_review_present,
                    max_wait_seconds,
                    ci_status,
                ),
            });
        } else {
            pending_actions.push(PendingAction::WasmEvent {
                event_type: "pr_review",
                payload: serde_json::json!({
                    "kind": review_event_kind_for_state(&ForgejoReviewVerdict::ChangesRequested)
                        .expect("changes_requested review state has an event kind"),
                    "pr_number": pr_number.as_u64(),
                    "branch": branch,
                    "comments": message,
                    "author_branch": review_author_branch(
                        reviews,
                        ForgejoReviewVerdict::ChangesRequested,
                    ),
                }),
            });
            pending_actions.push(PendingAction::WriteRegistryRounds {
                pr_number: pr_number.as_u64(),
                rounds: old_state.rounds,
            });
            pending_actions.push(PendingAction::EmitEvent {
                status: "copilot_review".to_string(),
                message: message.clone(),
                comments: Some(comments.to_vec()),
                reviews: Some(reviews.to_vec()),
            });
        }
    }

    let commented = reviews
        .iter()
        .any(|review| review.state == ForgejoReviewVerdict::Commented);
    let commented_fingerprint =
        review_fingerprint(comments, reviews, ForgejoReviewVerdict::Commented);
    if !approved
        && !changes_requested
        && commented
        && (old_state.last_review_state != ForgejoReviewVerdict::Commented
            || old_state.last_review_fingerprint != commented_fingerprint)
    {
        old_state.last_review_state = ForgejoReviewVerdict::Commented;
        old_state.last_review_fingerprint = commented_fingerprint;
        let message = format_message(comments, reviews);
        pending_actions.push(PendingAction::WasmEvent {
            event_type: "pr_review",
            payload: serde_json::json!({
                "kind": review_event_kind_for_state(&ForgejoReviewVerdict::Commented)
                    .expect("commented review state has an event kind"),
                "pr_number": pr_number.as_u64(),
                "branch": branch,
                "comments": message,
                "author_branch": review_author_branch(
                    reviews,
                    ForgejoReviewVerdict::Commented,
                ),
            }),
        });
    }

    if !approved && !changes_requested && observed_request_change_rounds > old_state.rounds {
        old_state.rounds = observed_request_change_rounds;
        if old_state.rounds >= max_rounds {
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
                    forgejo_review_present,
                    max_wait_seconds,
                    ci_status,
                ),
            });
        } else {
            pending_actions.push(PendingAction::WriteRegistryRounds {
                pr_number: pr_number.as_u64(),
                rounds: old_state.rounds,
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
            emitted_merge_ready_notification = true;
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

    if merge_ready_now && !emitted_merge_ready_notification {
        pending_actions.push(PendingAction::WasmEvent {
            event_type: "pr_review",
            payload: serde_json::json!({
                "kind": "merge_ready",
                "pr_number": pr_number.as_u64(),
                "ci_status": ci_status.as_str(),
                "branch": branch,
            }),
        });
    }

    if !old_state.notified_parent_timeout
        && !old_state.merge_ready_notified
        && old_state.first_seen.elapsed() > Duration::from_secs(max_wait_seconds)
    {
        let classification =
            classify_review_stall(old_state, reviewer_registered, forgejo_review_present);
        old_state.notified_parent_timeout = true;
        pending_actions.push(PendingAction::FileHumanEscalation {
            pr_number: pr_number.as_u64(),
            classification,
            diagnostic: review_stall_diagnostic(
                old_state,
                pr_sha,
                branch,
                reviewer_registered,
                forgejo_review_present,
                max_wait_seconds,
                ci_status,
            ),
        });
    }

    pending_actions
}

#[allow(dead_code)]
fn review_state_from_str(state: &str) -> ForgejoReviewVerdict {
    match state.to_ascii_lowercase().as_str() {
        "approved" | "approve" => ForgejoReviewVerdict::Approved,
        "changes_requested" | "request_changes" | "request_changes_requested" => {
            ForgejoReviewVerdict::ChangesRequested
        }
        "comment" | "commented" | "forgejo/comment" => ForgejoReviewVerdict::Commented,
        _ => ForgejoReviewVerdict::None,
    }
}

fn review_event_kind_for_state(state: &ForgejoReviewVerdict) -> Option<&'static str> {
    match state {
        ForgejoReviewVerdict::Approved => Some("approved"),
        ForgejoReviewVerdict::ChangesRequested => Some("review_received"),
        ForgejoReviewVerdict::Commented => Some("review_commented"),
        ForgejoReviewVerdict::None => None,
    }
}

fn aggregate_review_state(reviews: &[ForgejoReview]) -> ForgejoReviewState {
    if reviews
        .iter()
        .any(|review| review.state == ForgejoReviewVerdict::ChangesRequested)
    {
        ForgejoReviewState::ChangesRequested
    } else if reviews
        .iter()
        .any(|review| review.state == ForgejoReviewVerdict::Approved)
    {
        ForgejoReviewState::Approved
    } else if reviews
        .iter()
        .any(|review| review.state == ForgejoReviewVerdict::Commented)
    {
        ForgejoReviewState::Commented
    } else {
        ForgejoReviewState::PendingReview
    }
}

fn obs_to_review_parts(obs: &Observation) -> (Vec<ForgejoReview>, ForgejoReviewVerdict) {
    let state = match obs.review_state {
        ForgejoReviewState::Approved => ForgejoReviewVerdict::Approved,
        ForgejoReviewState::ChangesRequested => ForgejoReviewVerdict::ChangesRequested,
        ForgejoReviewState::Commented => ForgejoReviewVerdict::Commented,
        ForgejoReviewState::PendingReview => ForgejoReviewVerdict::None,
    };

    if !obs.reviews.is_empty() {
        return (obs.reviews.clone(), state);
    }

    let mut reviews: Vec<ForgejoReview> = obs
        .comments
        .iter()
        .map(|c| ForgejoReview {
            review_id: None,
            body: c.body.clone(),
            state: state.clone(),
            author_branch: c.author_branch.clone(),
            commit_id: None,
        })
        .collect();

    if obs.review_state == ForgejoReviewState::Approved && reviews.is_empty() {
        reviews.push(ForgejoReview {
            review_id: None,
            body: "Approved".to_string(),
            state: ForgejoReviewVerdict::Approved,
            author_branch: None,
            commit_id: None,
        });
    } else if obs.review_state == ForgejoReviewState::ChangesRequested && reviews.is_empty() {
        reviews.push(ForgejoReview {
            review_id: None,
            body: "Changes requested".to_string(),
            state: ForgejoReviewVerdict::ChangesRequested,
            author_branch: None,
            commit_id: None,
        });
    } else if obs.review_state == ForgejoReviewState::Commented && reviews.is_empty() {
        reviews.push(ForgejoReview {
            review_id: None,
            body: "Commented".to_string(),
            state: ForgejoReviewVerdict::Commented,
            author_branch: None,
            commit_id: None,
        });
    }

    (reviews, state)
}

fn review_author_branch(reviews: &[ForgejoReview], state: ForgejoReviewVerdict) -> Option<&str> {
    reviews
        .iter()
        .rev()
        .find(|review| review.state == state)
        .and_then(|review| review.author_branch.as_deref())
}

fn review_fingerprint(
    comments: &[ForgejoReviewComment],
    reviews: &[ForgejoReview],
    state: ForgejoReviewVerdict,
) -> Option<String> {
    reviews
        .iter()
        .rev()
        .find(|review| review.state == state)
        .map(|review| {
            let comment_fingerprint = comments
                .iter()
                .map(|comment| {
                    format!(
                        "{}\0{}\0{}",
                        comment.path.as_deref().unwrap_or_default(),
                        comment.thread_id.as_deref().unwrap_or_default(),
                        comment.body
                    )
                })
                .collect::<Vec<_>>()
                .join("\0");
            format!(
                "{}\0{}\0{}\0{}\0{}\0{}",
                review
                    .review_id
                    .map(|id| id.to_string())
                    .unwrap_or_default(),
                review.commit_id.as_deref().unwrap_or_default(),
                review.author_branch.as_deref().unwrap_or_default(),
                state_name(&review.state),
                review.body,
                comment_fingerprint,
            )
        })
}

fn state_name(state: &ForgejoReviewVerdict) -> &'static str {
    match state {
        ForgejoReviewVerdict::None => "none",
        ForgejoReviewVerdict::Commented => "commented",
        ForgejoReviewVerdict::ChangesRequested => "changes_requested",
        ForgejoReviewVerdict::Approved => "approved",
    }
}

fn signals_within_merge_ready_window(
    review_approved_at: Option<Instant>,
    ci_mergeable_at: Option<Instant>,
) -> bool {
    review_approved_at.is_some() && ci_mergeable_at.is_some()
}

fn classify_review_stall(
    state: &WatchState,
    reviewer_registered: bool,
    forgejo_review_present: bool,
) -> ReviewStallKind {
    if state.last_review_state == ForgejoReviewVerdict::ChangesRequested {
        return ReviewStallKind::DevNotPushing;
    }

    if state.addressed_changes && forgejo_review_present {
        return ReviewStallKind::ReviewerNotResponding;
    }

    if reviewer_registered && !forgejo_review_present {
        return ReviewStallKind::ReviewerNeverStarted;
    }

    ReviewStallKind::ReviewerNotResponding
}

fn review_stall_diagnostic(
    state: &WatchState,
    head_sha: &str,
    branch: &str,
    reviewer_registered: bool,
    forgejo_review_present: bool,
    wait_seconds: u64,
    ci_status: CIStatus,
) -> ReviewStallDiagnostic {
    ReviewStallDiagnostic {
        branch: branch.to_string(),
        head_sha: head_sha.to_string(),
        last_observed_sha: state.last_sha.clone(),
        rounds: state.rounds,
        reviewer_registered,
        forgejo_review_present,
        wait_seconds,
        ci_status: ci_status.to_string(),
    }
}
fn requests_merge_ready_parent_delivery(event_type: &str, payload: &serde_json::Value) -> bool {
    match event_type {
        "pr_review" => payload.get("kind").and_then(|value| value.as_str()) == Some("merge_ready"),
        "ci_status" => payload
            .get("merge_ready")
            .and_then(|value| value.as_bool())
            .unwrap_or(false),
        _ => false,
    }
}

fn is_review_feedback_event(event_type: &str, payload: &serde_json::Value) -> bool {
    event_type == "pr_review"
        && matches!(
            value_str(payload, "kind"),
            Some("review_received") | Some("reviewer_requested_changes") | Some("review_commented")
        )
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

fn format_review_message(comments: &[ForgejoReviewComment], reviews: &[ForgejoReview]) -> String {
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

fn format_observations(observations: &HashMap<u64, Observation>) -> String {
    let mut entries: Vec<_> = observations
        .iter()
        .map(|(number, observation)| {
            format!(
                "PR#{} review={:?} ci={:?}",
                number, observation.review_state, observation.ci_status
            )
        })
        .collect();
    entries.sort();
    entries.join(", ")
}

/// Extracts the branch name and head SHA from a pipeline event payload.
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

/// Extracts the pipeline rkey and status string from a pipeline status event payload.
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

    #[test]
    fn test_inbox_poke_message() {
        assert_eq!(
            inbox_poke_message(3),
            "You have 3 unread message(s). Call check_inbox."
        );
    }

    #[test]
    fn test_native_leaf_fallback_notifies_parent_for_review_received() {
        let payload = serde_json::json!({
            "kind": "review_received",
            "pr_number": 42,
            "branch": "main.feature-codex",
            "author_branch": "main.review-pr-42-claude",
            "comments": "Fix the failing assertion",
        });

        match native_event_action("pr_review", &payload, "dev") {
            Some(EventActionResponse::NotifyParent { message, pr_number }) => {
                assert_eq!(pr_number, 42);
                assert!(message.contains("PR #42 on branch main.feature-codex"));
                assert!(message.contains("changes requested"));
                assert!(message.contains("main.review-pr-42-claude"));
                assert!(message.contains("Fix the failing assertion"));
            }
            other => panic!("expected NotifyParent fallback, got {other:?}"),
        }
    }

    #[test]
    fn test_native_leaf_fallback_notifies_parent_for_comment_only_review() {
        let payload = serde_json::json!({
            "kind": "review_commented",
            "pr_number": 43,
            "branch": "main.feature-codex",
            "author_branch": "main.review-pr-43-gemini",
            "comments": "Consider the error path.",
        });

        match native_event_action("pr_review", &payload, "dev") {
            Some(EventActionResponse::NotifyParent { message, pr_number }) => {
                assert_eq!(pr_number, 43);
                assert!(message.contains("comment-only"));
                assert!(message.contains("Consider the error path."));
            }
            other => panic!("expected NotifyParent fallback, got {other:?}"),
        }
    }

    #[test]
    fn test_native_leaf_fallback_notifies_parent_for_merge_ready() {
        let payload = serde_json::json!({
            "kind": "merge_ready",
            "pr_number": 43,
            "ci_status": "success",
            "branch": "main.feature-codex",
        });

        match native_event_action("pr_review", &payload, "dev") {
            Some(EventActionResponse::NotifyParent { message, pr_number }) => {
                assert_eq!(pr_number, 43);
                assert!(message.contains("MERGE READY"));
                assert!(message.contains("PR #43"));
            }
            other => panic!("expected merge-ready NotifyParent fallback, got {other:?}"),
        }
    }

    #[test]
    fn test_native_leaf_fallback_notifies_parent_for_ci_blocked() {
        let payload = serde_json::json!({
            "pr_number": 44,
            "status": "failure",
            "branch": "main.feature-codex",
            "merge_blocked_on_ci": true,
        });

        match native_event_action("ci_status", &payload, "dev") {
            Some(EventActionResponse::NotifyParent { message, pr_number }) => {
                assert_eq!(pr_number, 44);
                assert_eq!(
                    message,
                    "[CI BLOCKED: PR #44] CI finished with status failure on main.feature-codex. Dev leaf is staying alive and waiting for TL direction."
                );
            }
            other => panic!("expected CI blocked NotifyParent fallback, got {other:?}"),
        }
    }

    #[test]
    fn test_native_leaf_fallback_preserves_no_action_events() {
        let payload = serde_json::json!({
            "kind": "approved",
            "pr_number": 45,
        });

        match native_event_action("pr_review", &payload, "dev") {
            Some(EventActionResponse::NoAction) => {}
            other => panic!("expected NoAction fallback, got {other:?}"),
        }
    }

    #[test]
    fn test_native_tl_fallback_covers_pr_review_signals() {
        let cases = [
            (
                serde_json::json!({ "kind": "approved", "pr_number": 46 }),
                "[PR READY] PR #46",
            ),
            (
                serde_json::json!({ "kind": "timeout", "pr_number": 47, "minutes": 15 }),
                "[REVIEW TIMEOUT] PR #47",
            ),
            (
                serde_json::json!({ "kind": "fixes_pushed", "pr_number": 48, "ci_status": "success" }),
                "[FIXES PUSHED] PR #48",
            ),
            (
                serde_json::json!({ "kind": "commits_pushed", "pr_number": 49, "ci_status": "pending" }),
                "[COMMITS PUSHED] PR #49",
            ),
            (
                serde_json::json!({ "kind": "stuck", "pr_number": 50, "rounds": 3 }),
                "[STUCK: 50, rounds=3]",
            ),
            (
                serde_json::json!({ "kind": "merge_ready", "pr_number": 51, "ci_status": "neutral", "branch": "main.subtl" }),
                "[MERGE READY] PR #51",
            ),
        ];

        for (payload, expected) in cases {
            match native_event_action("pr_review", &payload, "tl") {
                Some(EventActionResponse::InjectMessage { message }) => {
                    assert!(
                        message.contains(expected),
                        "message {message:?} missing {expected}"
                    );
                }
                other => panic!("expected TL InjectMessage fallback for {payload}, got {other:?}"),
            }
        }
    }

    #[test]
    fn test_native_tl_fallback_injects_merge_ready_ci_status() {
        let payload = serde_json::json!({
            "pr_number": 52,
            "status": "success",
            "branch": "main.subtl",
            "merge_ready": true,
        });

        match native_event_action("ci_status", &payload, "tl") {
            Some(EventActionResponse::InjectMessage { message }) => {
                assert!(message.contains("[MERGE READY] PR #52"));
            }
            other => panic!("expected TL CI InjectMessage fallback, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn test_no_plugin_dispatch_uses_native_fallback_for_non_wasm_dev_leaf() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();
        let plugins: PluginMap = Arc::new(RwLock::new(HashMap::new()));
        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_plugins(plugins);
        let payload = serde_json::json!({
            "kind": "merge_ready",
            "pr_number": 53,
            "ci_status": "success",
            "branch": "main.feature-shoal",
        });

        match watcher
            .call_handle_event_for_role(
                "main.feature-shoal",
                AgentType::Shoal,
                "dev",
                "pr_review",
                payload,
            )
            .await
            .unwrap()
        {
            Some(EventActionResponse::NotifyParent { message, pr_number }) => {
                assert_eq!(pr_number, 53);
                assert!(message.contains("[MERGE READY] PR #53"));
            }
            other => panic!("expected native NotifyParent fallback, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn test_no_plugin_dispatch_uses_native_fallback_for_process_agent() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();
        let plugins: PluginMap = Arc::new(RwLock::new(HashMap::new()));
        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_plugins(plugins);
        let payload = serde_json::json!({
            "kind": "merge_ready",
            "pr_number": 54,
            "ci_status": "success",
            "branch": "main.feature-process",
        });

        match watcher
            .call_handle_event_for_role(
                "main.feature-process",
                AgentType::Process,
                "process",
                "pr_review",
                payload,
            )
            .await
            .unwrap()
        {
            Some(EventActionResponse::NotifyParent { message, pr_number }) => {
                assert_eq!(pr_number, 54);
                assert!(message.contains("[MERGE READY] PR #54"));
            }
            other => panic!("expected native Process NotifyParent fallback, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn test_no_plugin_dispatch_uses_native_fallback_for_sub_tl() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();
        let plugins: PluginMap = Arc::new(RwLock::new(HashMap::new()));
        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_plugins(plugins);
        let payload = serde_json::json!({
            "kind": "fixes_pushed",
            "pr_number": 55,
            "ci_status": "pending",
        });

        match watcher
            .call_handle_event_for_role(
                "main.subtl-codex",
                AgentType::Codex,
                "tl",
                "pr_review",
                payload,
            )
            .await
            .unwrap()
        {
            Some(EventActionResponse::InjectMessage { message }) => {
                assert!(message.contains("[FIXES PUSHED] PR #55"));
            }
            other => panic!("expected native sub-TL InjectMessage fallback, got {other:?}"),
        }
    }

    fn test_state(branch: &BranchName, agent_type: AgentType, sha: &str) -> WatchState {
        WatchState::new(branch, agent_type, sha, CIStatus::Unknown, 0)
    }

    fn test_comment(body: &str) -> ForgejoReviewComment {
        ForgejoReviewComment {
            body: body.to_string(),
            path: None,
            diff_hunk: None,
            thread_id: None,
            resolved: false,
            author_branch: None,
        }
    }

    fn test_review(body: &str, state: ForgejoReviewVerdict) -> ForgejoReview {
        ForgejoReview {
            review_id: None,
            body: body.to_string(),
            state,
            author_branch: None,
            commit_id: None,
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
        state.last_review_state = ForgejoReviewVerdict::Approved;
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
        assert_eq!(state.last_review_state, ForgejoReviewVerdict::None);
        assert!(!state.notified_parent_approved);
        assert_eq!(state.review_approved_at, None);
    }

    #[test]
    fn test_first_approval_increments_review_rounds() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.ci_mergeable_at = Some(Instant::now());
        let reviews = vec![test_review("approved", ForgejoReviewVerdict::Approved)];

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
        state.last_review_state = ForgejoReviewVerdict::Approved;
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
        let reviews = vec![test_review("approved", ForgejoReviewVerdict::Approved)];
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
        state.last_review_state = ForgejoReviewVerdict::ChangesRequested;
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
        assert_eq!(state.last_review_state, ForgejoReviewVerdict::None);
    }

    #[test]
    fn test_reviewer_approval_triggers_manual_ci_when_status_unknown() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review("approved", ForgejoReviewVerdict::Approved)];

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
        state.last_review_state = ForgejoReviewVerdict::Approved;
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
    #[test]
    fn test_review_event_target_uses_pr_owner_not_ephemeral_reviewer() {
        let mut pr = test_pr_entry();
        pr.reviewer_agent = Some("review-pr-1-codex".to_string());
        pr.reviewer_birth_branch = Some("review-pr-1".to_string());

        let (branch, agent_type, role) = review_event_target(&pr);

        assert_eq!(branch.as_str(), "main.feat-gemini");
        assert_eq!(agent_type, AgentType::Gemini);
        assert_eq!(role, "dev");
        assert_ne!(branch.as_str(), "review-pr-1");
    }

    #[test]
    fn test_only_wasm_event_targets_keep_missing_plugin_at_error_level() {
        assert!(event_target_has_wasm_runtime(AgentType::Claude));
        assert!(event_target_has_wasm_runtime(AgentType::Gemini));
        assert!(!event_target_has_wasm_runtime(AgentType::Codex));
        assert!(!event_target_has_wasm_runtime(AgentType::OpenCode));
        assert!(!event_target_has_wasm_runtime(AgentType::Shoal));
        assert!(!event_target_has_wasm_runtime(AgentType::Process));
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
            ForgejoReviewVerdict::ChangesRequested,
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
        assert_eq!(
            state.last_review_state,
            ForgejoReviewVerdict::ChangesRequested
        );
    }

    #[test]
    fn test_changes_requested_payload_records_review_author_branch() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![ForgejoReview {
            review_id: None,
            body: "Please address comments".to_string(),
            state: ForgejoReviewVerdict::ChangesRequested,
            author_branch: Some("review-pr-1".to_string()),
            commit_id: None,
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
                    && payload["branch"] == "main.feat-gemini"
                    && payload["author_branch"] == "review-pr-1"
        )));
    }

    #[test]
    fn test_new_review_same_kind_emits_once_per_fingerprint() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let first = ForgejoReview {
            review_id: Some(10),
            body: "Address the error path".to_string(),
            state: ForgejoReviewVerdict::ChangesRequested,
            author_branch: Some("main.review-pr-10-codex".to_string()),
            commit_id: Some("abc123".to_string()),
        };
        let first_actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            std::slice::from_ref(&first),
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, reviews| reviews[0].body.clone(),
            5,
        );
        assert_eq!(
            first_actions
                .iter()
                .filter(|action| matches!(
                    action,
                    PendingAction::WasmEvent { payload, .. }
                        if payload["kind"] == "review_received"
                ))
                .count(),
            1
        );

        let duplicate = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            std::slice::from_ref(&first),
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, reviews| reviews[0].body.clone(),
            5,
        );
        assert!(!duplicate.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. }
                if payload["kind"] == "review_received"
        )));

        let second = ForgejoReview {
            review_id: Some(11),
            body: "Also handle the timeout path".to_string(),
            ..first
        };
        let second_actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            std::slice::from_ref(&second),
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, reviews| reviews[0].body.clone(),
            5,
        );
        assert!(second_actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. }
                if payload["kind"] == "review_received"
                    && payload["comments"] == "Also handle the timeout path"
        )));
    }

    #[test]
    fn test_approval_fires_approved() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review("LGTM!", ForgejoReviewVerdict::Approved)];
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
        assert!(!actions
            .iter()
            .any(|a| matches!(a, PendingAction::WasmEvent { payload, .. }
            if payload["kind"] == "merge_ready")));
        assert!(state.notified_parent_approved);
    }

    #[test]
    fn test_approval_with_unknown_ci_does_not_fire_merge_ready() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review("LGTM!", ForgejoReviewVerdict::Approved)];

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            &reviews,
            CIStatus::Unknown,
            true,
            branch.as_str(),
            &|_, _| String::new(),
            5,
        );

        assert!(actions.iter().any(|a| matches!(
            a,
            PendingAction::WasmEvent { payload, .. } if payload["kind"] == "approved"
        )));
        assert!(!actions.iter().any(|a| matches!(
            a,
            PendingAction::WasmEvent { payload, .. } if payload["kind"] == "merge_ready"
        )));
        assert!(!state.merge_ready_notified);
    }

    #[test]
    fn test_approval_after_green_ci_fires_merge_ready() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.last_ci_status = CIStatus::Success;
        state.ci_mergeable_at = Some(Instant::now() - Duration::from_secs(60));
        let reviews = vec![test_review("LGTM!", ForgejoReviewVerdict::Approved)];

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

        let pr_review_kinds: Vec<&str> = actions
            .iter()
            .filter_map(|a| match a {
                PendingAction::WasmEvent {
                    event_type: "pr_review",
                    payload,
                } => payload.get("kind").and_then(|kind| kind.as_str()),
                _ => None,
            })
            .collect();
        assert_eq!(pr_review_kinds, vec!["approved", "merge_ready"]);
        assert!(actions.iter().any(|a| matches!(
            a,
            PendingAction::WasmEvent {
                event_type: "pr_review",
                payload,
            } if payload["kind"] == "merge_ready" && payload["ci_status"] == "success"
        )));
        assert!(!state.merge_ready_notified);
    }

    #[test]
    fn test_initial_approved_green_ci_observation_fires_merge_ready() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review("LGTM!", ForgejoReviewVerdict::Approved)];

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
        assert!(!state.merge_ready_notified);
    }

    #[test]
    fn test_initial_approved_neutral_ci_observation_fires_merge_ready() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review("LGTM!", ForgejoReviewVerdict::Approved)];

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
        assert!(!state.merge_ready_notified);
    }

    #[test]
    fn test_merge_ready_retries_until_delivery_marks_notified() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.notified_parent_approved = true;
        state.last_review_state = ForgejoReviewVerdict::Approved;
        state.last_ci_status = CIStatus::Success;
        state.review_approved_at = Some(Instant::now() - Duration::from_secs(60));
        state.ci_mergeable_at = Some(Instant::now() - Duration::from_secs(60));

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
                event_type: "pr_review",
                payload,
            } if payload["kind"] == "merge_ready"
        )));
        assert!(
            !state.merge_ready_notified,
            "pure compute must not mark merge_ready_notified before async delivery succeeds"
        );
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
        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_ci_source_configured(true);
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
        state.last_review_state = ForgejoReviewVerdict::Approved;
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
        assert!(!state.merge_ready_notified);
    }

    #[test]
    fn test_green_ci_without_approval_does_not_fire_merge_ready() {
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
            } if payload["status"] == "success" && payload["merge_ready"] == false
        )));
        assert!(!state.merge_ready_notified);
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
    fn test_green_ci_after_existing_approval_fires_merge_ready() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.notified_parent_approved = true;
        state.last_review_state = ForgejoReviewVerdict::Approved;
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

        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent {
                event_type: "ci_status",
                payload,
            } if payload["status"] == "success" && payload["merge_ready"] == true
        )));
        assert!(!actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. } if payload["kind"] == "merge_ready"
        )));
        assert!(!state.merge_ready_notified);
    }

    #[test]
    fn test_changes_requested_fires_review_received() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let reviews = vec![test_review(
            "Needs work",
            ForgejoReviewVerdict::ChangesRequested,
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
            &|_, _| String::new(),
            5,
        );
        assert_eq!(
            state.last_review_state,
            ForgejoReviewVerdict::ChangesRequested
        );
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
            ForgejoReviewVerdict::ChangesRequested,
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
            2,
        );

        assert!(state.stuck);
        assert_eq!(state.rounds, 2);
        assert!(!actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. }
                if payload["kind"] == "review_received"
                    && payload["comments"] == "Still needs work"
        )));
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
    fn test_two_pushes_between_polls_counts_stale_changes_requested_rounds() {
        let branch = BranchName::try_from_str("main.feat-codex")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Codex, "sha1");
        state.last_review_state = ForgejoReviewVerdict::ChangesRequested;
        state.rounds = 1;

        let actions = compute_pr_actions_with_context(
            &mut state,
            PRNumber::new(1),
            "sha3",
            &[],
            &[],
            2,
            CIStatus::Unknown,
            false,
            true,
            true,
            branch.as_str(),
            &|_, _| String::new(),
            2,
            15 * 60,
        );

        assert_eq!(state.last_sha, "sha3");
        assert!(state.addressed_changes);
        assert!(state.stuck);
        assert_eq!(state.rounds, 2);
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. } if payload["kind"] == "fixes_pushed"
        )));
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
                diagnostic,
            } if diagnostic.rounds == 2 && diagnostic.head_sha == "sha3"
        )));
    }

    #[test]
    fn test_request_changes_then_approve_does_not_trip_stuck() {
        let branch = BranchName::try_from_str("main.feat-codex")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Codex, "abc123");
        let request_changes = vec![test_review(
            "Add required header",
            ForgejoReviewVerdict::ChangesRequested,
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
            test_review(
                "Add required header",
                ForgejoReviewVerdict::ChangesRequested,
            ),
            test_review("Approved", ForgejoReviewVerdict::Approved),
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
        assert_eq!(state.last_review_state, ForgejoReviewVerdict::Approved);
        assert!(actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. }
                if payload["kind"] == "approved" || payload["kind"] == "merge_ready"
        )));
        assert!(!actions.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. } if payload["kind"] == "stuck"
        )));
        assert!(!actions
            .iter()
            .any(|action| matches!(action, PendingAction::FileHumanEscalation { .. })));
    }

    #[test]
    fn test_request_changes_history_preserves_round_when_poll_sees_only_approval() {
        let branch = BranchName::try_from_str("main.feat-codex")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Codex, "def456");
        let reviews = vec![
            test_review(
                "Add required header",
                ForgejoReviewVerdict::ChangesRequested,
            ),
            test_review("Approved", ForgejoReviewVerdict::Approved),
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
        assert_eq!(state.last_review_state, ForgejoReviewVerdict::Approved);
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
    fn test_approved_pr_without_merge_ready_delivery_can_timeout() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.notified_parent_approved = true;
        state.last_review_state = ForgejoReviewVerdict::Approved;
        state.last_ci_status = CIStatus::Success;
        state.review_approved_at =
            Some(Instant::now() - MERGE_READY_SIGNAL_WINDOW - Duration::from_secs(60));
        state.ci_mergeable_at =
            Some(Instant::now() - MERGE_READY_SIGNAL_WINDOW - Duration::from_secs(60));
        state.first_seen = Instant::now() - Duration::from_secs(16 * 60);

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
            PendingAction::FileHumanEscalation {
                classification: ReviewStallKind::ReviewerNotResponding,
                ..
            }
        )));
        assert!(state.notified_parent_timeout);
    }

    #[test]
    fn test_merge_ready_delivery_suppresses_timeout_after_approval() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.notified_parent_approved = true;
        state.last_review_state = ForgejoReviewVerdict::Approved;
        state.merge_ready_notified = true;
        state.last_ci_status = CIStatus::Success;
        state.first_seen = Instant::now() - Duration::from_secs(16 * 60);

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
        assert!(!state.notified_parent_timeout);
    }

    #[test]
    fn test_stale_guard_suppresses_after_stuck() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        state.stuck = true;
        state.rounds = 2;
        let reviews = vec![test_review("Late approval", ForgejoReviewVerdict::Approved)];

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
        state.last_review_state = ForgejoReviewVerdict::Approved;
        state.notified_parent_approved = true;
        let reviews = vec![test_review(
            "Still approved",
            ForgejoReviewVerdict::Approved,
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
        let reviews = vec![ForgejoReview {
            review_id: None,
            body: "I have reviewed this and it is APPROVED".to_string(),
            state: ForgejoReviewVerdict::None,
            author_branch: None,
            commit_id: None,
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
            0,
            CIStatus::Unknown,
            false,
            true,
            true,
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

        state.last_review_state = ForgejoReviewVerdict::ChangesRequested;
        assert_eq!(
            classify_review_stall(&state, true, true),
            ReviewStallKind::DevNotPushing
        );

        state.last_review_state = ForgejoReviewVerdict::None;
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
            review_state: ForgejoReviewState::PendingReview,
            comments: vec![],
            reviews: vec![],
            changes_requested_rounds: 0,
            ci_status: CIStatus::Unknown,
            forgejo_review_present: false,
        };
        let (reviews, state) = obs_to_review_parts(&obs);
        assert_eq!(state, ForgejoReviewVerdict::None);
        assert!(reviews.is_empty());
    }

    #[test]
    fn test_obs_to_review_parts_approved_with_no_comments_creates_synthetic() {
        let obs = Observation {
            head_sha: "abc".into(),
            review_state: ForgejoReviewState::Approved,
            comments: vec![],
            reviews: vec![],
            changes_requested_rounds: 0,
            ci_status: CIStatus::Unknown,
            forgejo_review_present: false,
        };
        let (reviews, state) = obs_to_review_parts(&obs);
        assert_eq!(state, ForgejoReviewVerdict::Approved);
        assert!(reviews
            .iter()
            .any(|r| r.state == ForgejoReviewVerdict::Approved));
    }

    #[test]
    fn test_obs_to_review_parts_changes_requested() {
        let obs = Observation {
            head_sha: "abc".into(),
            review_state: ForgejoReviewState::ChangesRequested,
            comments: vec![],
            reviews: vec![],
            changes_requested_rounds: 1,
            ci_status: CIStatus::Unknown,
            forgejo_review_present: false,
        };
        let (reviews, state) = obs_to_review_parts(&obs);
        assert_eq!(state, ForgejoReviewVerdict::ChangesRequested);
        assert!(reviews
            .iter()
            .any(|r| r.state == ForgejoReviewVerdict::ChangesRequested));
    }

    #[test]
    fn test_obs_to_review_parts_retains_comment_review_body() {
        let review = ForgejoReview {
            review_id: Some(42),
            body: "A comment-only review body".to_string(),
            state: ForgejoReviewVerdict::Commented,
            author_branch: Some("main.review-pr-1-codex".to_string()),
            commit_id: None,
        };
        let obs = Observation {
            head_sha: "abc".into(),
            review_state: ForgejoReviewState::Commented,
            comments: vec![],
            reviews: vec![review],
            changes_requested_rounds: 0,
            ci_status: CIStatus::Unknown,
            forgejo_review_present: true,
        };
        let (reviews, state) = obs_to_review_parts(&obs);
        assert_eq!(state, ForgejoReviewVerdict::Commented);
        assert_eq!(reviews.len(), 1);
        assert_eq!(reviews[0].body, "A comment-only review body");
        assert_eq!(reviews[0].review_id, Some(42));
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
            ForgejoReview {
                review_id: None,
                body: "LGTM!".to_string(),
                state: ForgejoReviewVerdict::Approved,
                author_branch: None,
                commit_id: None,
            },
            ForgejoReview {
                review_id: None,
                body: "Good work.".to_string(),
                state: ForgejoReviewVerdict::None,
                author_branch: None,
                commit_id: None,
            },
        ];
        let msg = format_review_message(&[], &reviews);
        assert!(msg.contains("Review summary:"));
        assert!(msg.contains("LGTM!"));
        assert!(msg.contains("Good work."));
    }

    #[test]
    fn test_format_message_with_inline_comments() {
        let comments = vec![ForgejoReviewComment {
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
        assert_eq!(state.last_review_state, ForgejoReviewVerdict::None);
        assert!(!state.notified_parent_approved);
        assert!(!state.notified_parent_timeout);
        assert!(!state.addressed_changes);
    }

    #[test]
    fn test_review_state_dispatch_kind_mapping() {
        assert_eq!(
            review_event_kind_for_state(&ForgejoReviewVerdict::ChangesRequested),
            Some("review_received")
        );
        assert_eq!(
            review_event_kind_for_state(&ForgejoReviewVerdict::Approved),
            Some("approved")
        );
        assert_eq!(
            review_event_kind_for_state(&ForgejoReviewVerdict::None),
            None
        );
        assert_eq!(
            review_event_kind_for_state(&ForgejoReviewVerdict::Commented),
            Some("review_commented")
        );
    }

    #[test]
    fn test_comment_review_state_parsing_and_precedence() {
        for state in ["COMMENT", "comment", "commented", "Forgejo/COMMENT"] {
            assert_eq!(
                review_state_from_str(state),
                ForgejoReviewVerdict::Commented
            );
        }

        let commented = test_review(
            "Looks good with a small suggestion",
            ForgejoReviewVerdict::Commented,
        );
        assert_eq!(
            aggregate_review_state(std::slice::from_ref(&commented)),
            ForgejoReviewState::Commented
        );
        assert_eq!(
            aggregate_review_state(&[
                commented.clone(),
                test_review("approved", ForgejoReviewVerdict::Approved),
            ]),
            ForgejoReviewState::Approved
        );
        assert_eq!(
            aggregate_review_state(&[
                commented,
                test_review("needs work", ForgejoReviewVerdict::ChangesRequested),
            ]),
            ForgejoReviewState::ChangesRequested
        );
    }

    #[test]
    fn test_comment_review_is_retained_and_emits_body_once() {
        let branch = BranchName::try_from_str("main.feat-gemini")
            .expect("literal validated string is non-empty");
        let mut state = test_state(&branch, AgentType::Gemini, "abc123");
        let mut comment = test_review(
            "Looks good with a small suggestion",
            ForgejoReviewVerdict::Commented,
        );
        comment.author_branch = Some("main.review-pr-1-codex".to_string());

        let actions = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            std::slice::from_ref(&comment),
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, reviews| reviews[0].body.clone(),
            5,
        );
        let payload = actions
            .iter()
            .find_map(|action| match action {
                PendingAction::WasmEvent { payload, .. }
                    if payload["kind"] == "review_commented" =>
                {
                    Some(payload)
                }
                _ => None,
            })
            .expect("comment-only review should emit review_commented");
        assert_eq!(payload["comments"], comment.body);
        assert_eq!(payload["author_branch"], "main.review-pr-1-codex");
        assert_eq!(state.last_review_state, ForgejoReviewVerdict::Commented);

        let duplicate = compute_pr_actions(
            &mut state,
            PRNumber::new(1),
            "abc123",
            &[],
            std::slice::from_ref(&comment),
            CIStatus::Unknown,
            false,
            branch.as_str(),
            &|_, reviews| reviews[0].body.clone(),
            5,
        );
        assert!(!duplicate.iter().any(|action| matches!(
            action,
            PendingAction::WasmEvent { payload, .. }
                if payload["kind"] == "review_commented"
        )));
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
            _pr: &crate::services::pr_registry::PrEntry,
        ) -> anyhow::Result<()> {
            self.called.store(true, std::sync::atomic::Ordering::SeqCst);
            Ok(())
        }
    }

    fn test_pr_entry() -> crate::services::pr_registry::PrEntry {
        crate::services::pr_registry::PrEntry {
            number: 1,
            head_branch: "main.feat-gemini".to_string(),
            base_branch: "main".to_string(),
            title: "Test PR".to_string(),
            body: String::new(),
            author_agent: "feat-gemini".to_string(),
            author_role: "dev".to_string(),
            created_at: chrono::Utc::now(),
            state: crate::services::pr_registry::PrState::Open,
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
        pr: crate::services::pr_registry::PrEntry,
    ) -> crate::services::pr_registry::PrRegistry {
        let mut prs = HashMap::new();
        prs.insert(pr.number, pr);
        crate::services::pr_registry::PrRegistry {
            prs,
            next_number: 2,
        }
    }

    fn pr_with_reviewer(
        pr_number: u64,
        agent_name: &str,
        birth_branch: &str,
    ) -> crate::services::pr_registry::PrEntry {
        let mut pr = test_pr_entry();
        pr.number = pr_number;
        pr.reviewer_agent = Some(agent_name.to_string());
        pr.reviewer_birth_branch = Some(birth_branch.to_string());
        pr
    }

    fn test_observation(sha: &str) -> Observation {
        Observation {
            head_sha: sha.to_string(),
            review_state: crate::services::pr_registry::ForgejoReviewState::PendingReview,
            comments: vec![],
            reviews: vec![],
            changes_requested_rounds: 0,
            ci_status: CIStatus::Unknown,
            forgejo_review_present: false,
        }
    }

    fn terminal_changes_requested_observation(sha: &str) -> Observation {
        Observation {
            head_sha: sha.to_string(),
            review_state: crate::services::pr_registry::ForgejoReviewState::ChangesRequested,
            comments: vec![],
            reviews: vec![test_review(
                "Still needs work",
                ForgejoReviewVerdict::ChangesRequested,
            )],
            changes_requested_rounds: 2,
            ci_status: CIStatus::Unknown,
            forgejo_review_present: true,
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
    async fn test_process_observations_does_not_write_head_sha_registry() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services));
        let pr = test_pr_entry();
        let registry = test_registry(pr);

        let mut observations = HashMap::new();
        observations.insert(1u64, test_observation("def456"));

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();

        assert!(!temp_dir.path().join(".exo/prs.json").exists());
    }

    #[test]
    fn evict_closed_prs_from_state_removes_prs_missing_from_open_registry() {
        let mut state = WatcherStateFile::default();
        state.prs.insert(
            1,
            WatcherPrState {
                rounds: 1,
                stuck: true,
                needs_human_review: true,
                ..Default::default()
            },
        );
        state.prs.insert(
            2,
            WatcherPrState {
                rounds: 2,
                stuck: false,
                needs_human_review: false,
                ..Default::default()
            },
        );
        state.prs.insert(
            3,
            WatcherPrState {
                rounds: 3,
                stuck: true,
                needs_human_review: false,
                ..Default::default()
            },
        );
        let mut registry = PrRegistry::default();
        let mut pr = test_pr_entry();
        pr.number = 2;
        registry.prs.insert(2, pr);

        let evicted = evict_closed_prs_from_state(&mut state, &registry);

        assert_eq!(evicted, vec![1, 3]);
        assert_eq!(state.prs.keys().copied().collect::<Vec<_>>(), vec![2]);
    }

    #[tokio::test]
    async fn watcher_runtime_state_resets_review_cycle_flags() {
        let runtime_state = WatcherRuntimeState::new();
        let branch = BranchName::try_from_str("main.feature").unwrap();
        let mut watch_state =
            WatchState::new(&branch, AgentType::Codex, "abc123", CIStatus::Failure, 2);
        watch_state.notified_parent_timeout = true;
        watch_state.notified_parent_approved = true;
        watch_state.merge_ready_notified = true;
        watch_state.addressed_changes = true;
        watch_state.rounds = 3;
        watch_state.stuck = true;
        watch_state.reviewer_spawned = true;
        watch_state.reviewer_disposed = true;
        watch_state.review_approved_at = Some(Instant::now());
        watch_state.ci_mergeable_at = Some(Instant::now());
        watch_state.ci_triggered_sha = Some("abc123".to_string());
        watch_state.ci_blocked_notified = true;
        runtime_state.prs.lock().await.insert(7, watch_state);

        assert!(runtime_state.reset_review_cycle(7).await);
        assert!(!runtime_state.reset_review_cycle(8).await);

        let state = runtime_state.prs.lock().await;
        let reset = state.get(&7).unwrap();
        assert!(!reset.notified_parent_timeout);
        assert!(!reset.notified_parent_approved);
        assert!(!reset.merge_ready_notified);
        assert!(!reset.addressed_changes);
        assert_eq!(reset.rounds, 0);
        assert!(!reset.stuck);
        assert!(!reset.reviewer_spawned);
        assert!(!reset.reviewer_disposed);
        assert!(reset.review_approved_at.is_none());
        assert!(reset.ci_mergeable_at.is_none());
        assert!(reset.ci_triggered_sha.is_none());
        assert!(!reset.ci_blocked_notified);
    }

    #[tokio::test]
    async fn test_process_observations_preserves_persisted_terminal_state_on_approval() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services));
        let mut state = WatcherStateFile::default();
        state.prs.insert(
            1,
            WatcherPrState {
                rounds: 2,
                stuck: true,
                needs_human_review: true,
                ..Default::default()
            },
        );
        watcher.write_watcher_state(&state).await.unwrap();
        let registry = test_registry(test_pr_entry());

        let mut observations = HashMap::new();
        observations.insert(
            1u64,
            Observation {
                head_sha: "abc123".to_string(),
                review_state: crate::services::pr_registry::ForgejoReviewState::Approved,
                comments: vec![],
                reviews: vec![],
                changes_requested_rounds: 0,
                ci_status: CIStatus::Unknown,
                forgejo_review_present: false,
            },
        );

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();

        let persisted = watcher.read_watcher_state().await.unwrap();
        let pr_state = persisted.prs.get(&1).unwrap();
        assert_eq!(pr_state.rounds, 2);
        assert!(pr_state.stuck);
        assert!(pr_state.needs_human_review);
        assert!(!temp_dir.path().join(".exo/prs.json").exists());
    }

    #[tokio::test]
    async fn test_process_observations_hydrates_persisted_terminal_runtime_state() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services));
        let mut persisted = WatcherStateFile::default();
        persisted.prs.insert(
            1,
            WatcherPrState {
                rounds: 3,
                stuck: true,
                needs_human_review: true,
                last_review_state: ForgejoReviewVerdict::ChangesRequested,
                last_head_sha: Some("abc123".to_string()),
                last_review_fingerprint: Some("persisted-review".to_string()),
                notified_parent_timeout: true,
                merge_ready_notified: true,
                ci_blocked_notified: true,
                ..Default::default()
            },
        );
        watcher.write_watcher_state(&persisted).await.unwrap();

        let registry = test_registry(test_pr_entry());
        let mut observations = HashMap::new();
        observations.insert(1u64, test_observation("abc123"));

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();

        let runtime = watcher.state.prs.lock().await;
        let state = runtime.get(&1).expect("persisted PR state should hydrate");
        assert_eq!(state.rounds, 3);
        assert!(state.stuck);
        assert_eq!(
            state.last_review_state,
            ForgejoReviewVerdict::ChangesRequested
        );
        assert_eq!(
            state.last_review_fingerprint.as_deref(),
            Some("persisted-review")
        );
        assert!(state.notified_parent_timeout);
        assert!(state.merge_ready_notified);
        assert!(state.ci_blocked_notified);
    }

    #[tokio::test]
    async fn test_terminal_review_handoff_does_not_create_chainlink_issue() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();
        let policy = ReviewPolicy {
            reviewer_max_rounds: 2,
            ..ReviewPolicy::default()
        };
        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_policy(policy);
        let registry = test_registry(test_pr_entry());
        let mut observations = HashMap::new();
        observations.insert(1u64, terminal_changes_requested_observation("abc123"));

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();

        assert!(
            !temp_dir.path().join(".chainlink").exists(),
            "watcher terminal handoff must not create a Chainlink directory"
        );
        assert!(watcher
            .state
            .prs
            .lock()
            .await
            .get(&1)
            .is_some_and(|state| state.stuck));
    }

    #[tokio::test]
    async fn test_restarted_terminal_review_does_not_replay_handoff() {
        let temp_dir = tempfile::tempdir().unwrap();
        let policy = ReviewPolicy {
            reviewer_max_rounds: 2,
            ..ReviewPolicy::default()
        };
        let registry = test_registry(test_pr_entry());
        let mut observations = HashMap::new();
        observations.insert(1u64, terminal_changes_requested_observation("abc123"));

        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();
        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_policy(policy.clone());
        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();
        let persisted_before_restart =
            tokio::fs::read_to_string(temp_dir.path().join(".exo/watcher-state.json"))
                .await
                .unwrap();
        drop(watcher);

        let mut restarted_services = crate::services::Services::test();
        restarted_services.project_dir = temp_dir.path().to_path_buf();
        let restarted_watcher =
            WorktreeEventWatcher::new(Arc::new(restarted_services)).with_policy(policy);
        restarted_watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();

        assert!(
            !temp_dir.path().join(".chainlink").exists(),
            "restarting the watcher must not create a Chainlink issue"
        );
        let persisted_after_restart =
            tokio::fs::read_to_string(temp_dir.path().join(".exo/watcher-state.json"))
                .await
                .unwrap();
        assert_eq!(persisted_after_restart, persisted_before_restart);
        assert!(restarted_watcher
            .state
            .prs
            .lock()
            .await
            .get(&1)
            .is_some_and(|state| state.stuck));
    }

    #[test]
    fn watcher_log_line_formatters_include_review_disposal_and_sha_drop_context() {
        assert_eq!(
            dropped_review_by_sha_log_line(7, "old-sha", "new-sha"),
            "dropped-review-by-SHA: PR #7 review commit old-sha does not match head new-sha"
        );
        assert_eq!(
            reviewer_disposal_log_line(7, &["review-pr-7-codex".to_string()]),
            "terminal review observed for PR #7; disposing reviewer slugs: review-pr-7-codex"
        );
        assert_eq!(
            reviewer_disposal_log_line(7, &[]),
            "terminal review observed for PR #7 but no reviewer slug matched for disposal"
        );
    }

    #[tokio::test]
    async fn test_process_observations_disposes_reviewer_when_restart_sees_approved_pr() {
        use std::sync::atomic::Ordering;

        let called = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let spawner = Arc::new(MockReviewerSpawner {
            called: called.clone(),
        });

        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let reviewer_slug = "review-pr-1-codex";
        let reviewer_worktree = temp_dir.path().join(".exo/worktrees").join(reviewer_slug);
        let reviewer_agent_dir = temp_dir.path().join(".exo/agents").join(reviewer_slug);
        tokio::fs::create_dir_all(&reviewer_worktree).await.unwrap();
        tokio::fs::create_dir_all(&reviewer_agent_dir)
            .await
            .unwrap();

        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_reviewer_spawner(spawner);
        let pr = pr_with_reviewer(1, reviewer_slug, "review-pr-1");
        let registry = test_registry(pr);

        let mut observations = HashMap::new();
        observations.insert(
            1u64,
            Observation {
                head_sha: "abc123".to_string(),
                review_state: crate::services::pr_registry::ForgejoReviewState::Approved,
                comments: vec![],
                reviews: vec![test_review("approved", ForgejoReviewVerdict::Approved)],
                changes_requested_rounds: 0,
                ci_status: CIStatus::Unknown,
                forgejo_review_present: true,
            },
        );

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        assert!(
            !called.load(Ordering::SeqCst),
            "already-approved PRs observed after restart must not spawn a fresh reviewer"
        );
        assert!(
            !reviewer_agent_dir.exists(),
            "already-approved PRs observed after restart should dispose reviewer agent resources"
        );
        let state = watcher.state.prs.lock().await;
        assert!(state.get(&1).is_some_and(|state| state.reviewer_disposed));
        drop(state);
        let watcher_log = tokio::fs::read_to_string(temp_dir.path().join(".exo/logs/watcher.log"))
            .await
            .unwrap();
        assert!(watcher_log.contains(
            "terminal review observed for PR #1; disposing reviewer slugs: review-pr-1-codex"
        ));
    }

    #[tokio::test]
    async fn test_process_observations_warns_when_terminal_review_has_no_reviewer_slug() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services));
        let pr = test_pr_entry();
        let registry = test_registry(pr);

        let mut observations = HashMap::new();
        observations.insert(
            1u64,
            Observation {
                head_sha: "abc123".to_string(),
                review_state: crate::services::pr_registry::ForgejoReviewState::Approved,
                comments: vec![],
                reviews: vec![test_review("approved", ForgejoReviewVerdict::Approved)],
                changes_requested_rounds: 0,
                ci_status: CIStatus::Unknown,
                forgejo_review_present: true,
            },
        );

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();

        let watcher_log = tokio::fs::read_to_string(temp_dir.path().join(".exo/logs/watcher.log"))
            .await
            .unwrap();
        assert!(watcher_log.contains(
            "terminal review observed for PR #1 but no reviewer slug matched for disposal"
        ));
    }

    #[tokio::test]
    async fn test_observed_ci_status_ignores_webhook_map_without_forgejo() {
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
        let watcher = WorktreeEventWatcher::new(Arc::new(services))
            .with_ci_status_map(ci_status_map)
            .with_ci_source_configured(true);

        assert_eq!(
            watcher.observed_ci_status(&branch, "abc123").await,
            CIStatus::Unknown
        );
    }

    #[tokio::test]
    async fn test_failed_reviewer_spawn_leaves_flag_false() {
        use std::sync::atomic::{AtomicBool, Ordering};

        struct FailingSpawner {
            called: Arc<AtomicBool>,
        }

        #[async_trait::async_trait]
        impl ReviewerSpawner for FailingSpawner {
            async fn spawn_reviewer_for_pr(
                &self,
                _pr: &crate::services::pr_registry::PrEntry,
            ) -> anyhow::Result<()> {
                self.called.store(true, Ordering::SeqCst);
                anyhow::bail!("spawn failed")
            }
        }

        let called = Arc::new(AtomicBool::new(false));
        let spawner = Arc::new(FailingSpawner {
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
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        assert!(called.load(Ordering::SeqCst));
        let state = watcher.state.prs.lock().await;
        assert!(
            !state.get(&1).map(|s| s.reviewer_spawned).unwrap_or(true),
            "reviewer_spawned should remain false after a failed spawn"
        );
    }

    #[tokio::test]
    async fn test_failed_reviewer_spawn_retries_without_head_sha_change() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        struct FailsOnceSpawner {
            count: Arc<AtomicUsize>,
        }

        #[async_trait::async_trait]
        impl ReviewerSpawner for FailsOnceSpawner {
            async fn spawn_reviewer_for_pr(
                &self,
                _pr: &crate::services::pr_registry::PrEntry,
            ) -> anyhow::Result<()> {
                let previous = self.count.fetch_add(1, Ordering::SeqCst);
                if previous == 0 {
                    anyhow::bail!("spawn failed")
                }
                Ok(())
            }
        }

        let call_count = Arc::new(AtomicUsize::new(0));
        let spawner = Arc::new(FailsOnceSpawner {
            count: call_count.clone(),
        });

        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services)).with_reviewer_spawner(spawner);
        let mut pr = test_pr_entry();
        pr.last_head_sha = Some("abc123".to_string());
        let registry = test_registry(pr);
        let mut observations = HashMap::new();
        observations.insert(1u64, test_observation("abc123"));

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        {
            let state = watcher.state.prs.lock().await;
            assert!(
                !state.get(&1).map(|s| s.reviewer_spawned).unwrap_or(true),
                "reviewer_spawned should remain false after the first failed spawn"
            );
        }

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        assert_eq!(call_count.load(Ordering::SeqCst), 2);
        let state = watcher.state.prs.lock().await;
        assert!(
            state.get(&1).map(|s| s.reviewer_spawned).unwrap_or(false),
            "reviewer_spawned should become true after retry succeeds"
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

        let state = watcher.state.prs.lock().await;
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
                _pr: &crate::services::pr_registry::PrEntry,
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
        watcher.state.prs.lock().await.insert(
            1,
            WatchState::new(&branch, AgentType::Codex, "abc123", CIStatus::Unknown, 0),
        );

        let mut pr = test_pr_entry();
        pr.last_head_sha = Some("def456".to_string());
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
    async fn test_reviewer_spawner_called_for_new_head_after_changes_requested() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        struct CountingSpawner {
            count: Arc<AtomicUsize>,
        }
        #[async_trait::async_trait]
        impl ReviewerSpawner for CountingSpawner {
            async fn spawn_reviewer_for_pr(
                &self,
                _pr: &crate::services::pr_registry::PrEntry,
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
        let mut watch_state =
            WatchState::new(&branch, AgentType::Codex, "abc123", CIStatus::Unknown, 0);
        watch_state.last_review_state = ForgejoReviewVerdict::ChangesRequested;
        watch_state.rounds = 1;
        watch_state.reviewer_spawned = true;
        watch_state.reviewer_disposed = true;
        watcher.state.prs.lock().await.insert(1, watch_state);

        let mut pr = test_pr_entry();
        pr.last_head_sha = Some("def456".to_string());
        let registry = test_registry(pr);
        let mut observations = HashMap::new();
        observations.insert(
            1u64,
            Observation {
                head_sha: "def456".to_string(),
                review_state: ForgejoReviewState::ChangesRequested,
                comments: vec![],
                reviews: vec![],
                changes_requested_rounds: 1,
                ci_status: CIStatus::Unknown,
                forgejo_review_present: false,
            },
        );

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        assert_eq!(call_count.load(Ordering::SeqCst), 1);
        let state = watcher.state.prs.lock().await;
        let pr_state = state.get(&1).unwrap();
        assert!(pr_state.reviewer_spawned);
        assert!(!pr_state.reviewer_disposed);
        assert_eq!(pr_state.last_review_state, ForgejoReviewVerdict::None);
        assert_eq!(pr_state.rounds, 1);
    }

    #[tokio::test]
    async fn test_reviewer_spawner_called_when_stale_changes_requested_review_was_missed() {
        use std::sync::atomic::{AtomicUsize, Ordering};

        struct CountingSpawner {
            count: Arc<AtomicUsize>,
        }

        #[async_trait::async_trait]
        impl ReviewerSpawner for CountingSpawner {
            async fn spawn_reviewer_for_pr(
                &self,
                _pr: &crate::services::pr_registry::PrEntry,
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
        let mut watch_state =
            WatchState::new(&branch, AgentType::Codex, "abc123", CIStatus::Unknown, 0);
        watch_state.reviewer_spawned = true;
        watch_state.reviewer_disposed = true;
        watcher.state.prs.lock().await.insert(1, watch_state);

        let mut pr = test_pr_entry();
        pr.last_head_sha = Some("def456".to_string());
        let registry = test_registry(pr);
        let mut observations = HashMap::new();
        observations.insert(
            1u64,
            Observation {
                head_sha: "def456".to_string(),
                review_state: ForgejoReviewState::PendingReview,
                comments: vec![],
                reviews: vec![],
                changes_requested_rounds: 1,
                ci_status: CIStatus::Unknown,
                forgejo_review_present: false,
            },
        );

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;

        assert_eq!(call_count.load(Ordering::SeqCst), 1);
        let state = watcher.state.prs.lock().await;
        let pr_state = state.get(&1).unwrap();
        assert!(pr_state.reviewer_spawned);
        assert!(!pr_state.reviewer_disposed);
        assert_eq!(pr_state.last_review_state, ForgejoReviewVerdict::None);
        assert_eq!(pr_state.rounds, 1);
        drop(state);

        let persisted = watcher.read_watcher_state().await.unwrap();
        let persisted_pr = persisted.prs.get(&1).unwrap();
        assert_eq!(persisted_pr.rounds, 1);
        assert_eq!(persisted_pr.last_head_sha.as_deref(), Some("def456"));
    }

    #[tokio::test]
    async fn test_process_observations_persists_last_observed_head_sha() {
        let temp_dir = tempfile::tempdir().unwrap();
        let mut services = crate::services::Services::test();
        services.project_dir = temp_dir.path().to_path_buf();

        let watcher = WorktreeEventWatcher::new(Arc::new(services));
        let mut pr = test_pr_entry();
        pr.last_head_sha = Some("def456".to_string());
        let registry = test_registry(pr);
        let mut observations = HashMap::new();
        observations.insert(1u64, test_observation("def456"));

        watcher
            .process_observations(&registry, &observations)
            .await
            .unwrap();

        let persisted = watcher.read_watcher_state().await.unwrap();
        assert_eq!(
            persisted
                .prs
                .get(&1)
                .and_then(|state| state.last_head_sha.as_deref()),
            Some("def456")
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
                _pr: &crate::services::pr_registry::PrEntry,
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
