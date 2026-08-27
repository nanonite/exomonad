//! Agent effect handler for the `agent.*` namespace.
//!
//! Uses proto-generated types from `exomonad_proto::effects::agent`.

use crate::domain::{
    AgentName, AgentPermissions, BirthBranch, BranchName, CIStatus, ClaudeSessionUuid, RoutingInfo,
    Slug, TeamName,
};
use crate::effects::{
    dispatch_agent_effect, AgentEffects, EffectError, EffectHandler, EffectResult, ResultExt,
    ResultExtPreserve,
};

use super::non_empty;
use crate::services::agent_control::{
    finish_invocation_and_tombstone, read_invocation, slugify, AgentControlService, AgentIdentity,
    AgentIdentityRecord, AgentInfo, AgentType as ServiceAgentType, ClaudeSpawnFlags,
    InvocationFinishResult, InvocationRecord, InvocationStatus, InvocationTrigger,
    RecoveryAuthorization, RecoveryInvocationLineage, SpawnLeafOptions, SpawnOptions,
    SpawnSubtreeOptions, SpawnWorkerOptions, Topology,
};
use crate::services::agent_resources::dispose_agent_resources;
use crate::services::configured_tl_preflight_runtime_paths;
use crate::services::continuation::composer::{prefix_task, resume_pr_prefix};
use crate::services::forgejo::{
    normalize_review_verdict, ForgejoPullRequest, ForgejoPullRequestReview,
};
use crate::services::pr_registry::{
    publication_history_for_slice, read_published_heads,
    resolve_live_pr_for_slice_with_abandonments, verify_current_publication_ownership,
    AbandonedAttempt, LivePrResolution, PrEntry, PrState, PublishedHead,
};
#[cfg(test)]
use crate::services::pr_registry::{PrRegistry, PublicationProvenance};
use crate::services::supervisor_registry::SupervisorInfo;
use crate::{GithubOwner, GithubRepo, IssueNumber, PRNumber};
use async_trait::async_trait;
use chrono::Utc;
use exomonad_proto::effects::agent::*;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::{fs, process::Command};
use tracing::{info, warn};

use crate::services::{
    capture_memory, HasAgentResolver, HasClaudeSessionRegistry, HasEventLog, HasForgejoClient,
    HasForgejoReviewerClient, HasGitHubClient, HasGitWorktreeService, HasInboxStore, HasProjectDir,
    HasSessionMemory, HasSupervisorRegistry, HasTeamRegistry, HasWatcherRuntimeState,
    MemoryCapture, MemoryKind,
};

/// Agent effect handler.
///
/// Handles all effects in the `agent.*` namespace by delegating to
/// the generated `dispatch_agent_effect` function.
pub struct AgentHandler<C> {
    service: Arc<AgentControlService<C>>,
    ctx: Arc<C>,
}

impl<
        C: HasTeamRegistry
            + HasAgentResolver
            + HasGitHubClient
            + HasProjectDir
            + HasGitWorktreeService
            + HasInboxStore
            + HasSessionMemory
            + HasSupervisorRegistry
            + HasClaudeSessionRegistry
            + HasEventLog
            + HasForgejoClient
            + HasWatcherRuntimeState
            + 'static,
    > AgentHandler<C>
{
    pub fn new(service: Arc<AgentControlService<C>>, ctx: Arc<C>) -> Self {
        Self { service, ctx }
    }

    /// Auto-register a spawned child in the SupervisorRegistry.
    /// Resolves the caller's team from TeamRegistry, then maps the child key
    /// to the caller as supervisor.
    async fn register_child_supervisor(
        &self,
        child_key: &str,
        ctx: &crate::effects::EffectContext,
    ) {
        let sup_reg = self.ctx.supervisor_registry();
        let team_reg = self.ctx.team_registry();
        let agent_key = ctx.agent_name.to_string();
        let team_name = if let Some(info) = team_reg.get(&agent_key).await {
            TeamName::try_from_str(info.team_name.as_str())
                .expect("validated string input is non-empty")
        } else if let Some(info) = team_reg.get(ctx.birth_branch.as_ref()).await {
            TeamName::try_from_str(info.team_name.as_str())
                .expect("validated string input is non-empty")
        } else {
            let fallback =
                TeamName::try_from_str(format!("exo-{}", ctx.birth_branch.as_ref()).as_str())
                    .expect("validated string input is non-empty");
            info!(
                agent = %agent_key,
                child = %child_key,
                team = %fallback,
                "No team found for agent — registering supervisor with synthetic team"
            );
            fallback
        };

        sup_reg
            .register(
                &[child_key.to_string()],
                SupervisorInfo {
                    supervisor: ctx.agent_name.clone(),
                    team: team_name,
                },
            )
            .await;
    }

    /// Register a spawned child as a synthetic member in the TL's actual team.
    ///
    /// Resolves the team from TeamRegistry (same pattern as `register_child_supervisor`)
    /// so the child is registered in the user-created team (e.g., "gh-issues"), not
    /// a hardcoded "exo-{branch}" team that CC doesn't recognize.
    async fn register_synthetic_member(
        &self,
        member_name: &AgentName,
        agent_type: &str,
        ctx: &crate::effects::EffectContext,
    ) {
        let team_reg = self.ctx.team_registry();
        let agent_key = ctx.agent_name.to_string();
        let team_name = if let Some(info) = team_reg.get(&agent_key).await {
            TeamName::try_from_str(info.team_name.as_str())
                .expect("validated string input is non-empty")
        } else if let Some(info) = team_reg.get(ctx.birth_branch.as_ref()).await {
            TeamName::try_from_str(info.team_name.as_str())
                .expect("validated string input is non-empty")
        } else {
            warn!(
                member = %member_name,
                "No team found — skipping synthetic member registration"
            );
            return;
        };
        if let Err(e) = crate::services::synthetic_members::register_synthetic_member(
            &team_name,
            member_name,
            agent_type,
        ) {
            warn!(
                member = %member_name,
                team = %team_name,
                error = %e,
                "Failed to register synthetic team member (non-fatal)"
            );
        }
    }

    async fn register_claude_team_child(
        &self,
        member_name: &AgentName,
        member_type: &str,
        supervisor_key: &str,
        ctx: &crate::effects::EffectContext,
    ) {
        self.register_synthetic_member(member_name, member_type, ctx)
            .await;

        let team_reg = self.ctx.team_registry();
        let agent_key = ctx.agent_name.to_string();
        let parent_team = match team_reg.get(&agent_key).await {
            Some(info) => Some(info),
            None => team_reg.get(ctx.birth_branch.as_ref()).await,
        };
        if let Some(parent_team) = parent_team {
            let team_info = claude_teams_bridge::TeamInfo {
                team_name: parent_team.team_name.clone(),
                inbox_name: member_name.to_string(),
            };
            let child_birth_branch = format!("{}.{}", ctx.birth_branch, member_name);
            team_reg
                .register(member_name.as_ref(), team_info.clone())
                .await;
            team_reg.register(supervisor_key, team_info.clone()).await;
            team_reg.register(&child_birth_branch, team_info).await;
        }

        self.register_child_supervisor(supervisor_key, ctx).await;
    }

    /// Propagate the parent's team registration to a spawned sub-TL's identity keys.
    ///
    /// Sub-TLs don't call TeamCreate — they're part of the parent's team. But when
    /// a sub-TL spawns workers, `register_synthetic_member` looks up the sub-TL's
    /// keys in TeamRegistry and finds nothing. This method bridges that gap by
    /// registering the sub-TL's keys (agent_name, birth_branch) pointing to the
    /// parent's team.
    async fn propagate_team_to_child(
        &self,
        child_branch: &str,
        child_agent_type: crate::services::agent_control::AgentType,
        ctx: &crate::effects::EffectContext,
    ) {
        let team_reg = self.ctx.team_registry();
        let agent_key = ctx.agent_name.to_string();
        let parent_team = if let Some(info) = team_reg.get(&agent_key).await {
            info
        } else if let Some(info) = team_reg.get(ctx.birth_branch.as_ref()).await {
            info
        } else {
            warn!(
                child = %child_branch,
                "No team found for parent — skipping team propagation to sub-TL"
            );
            return;
        };

        // Derive the sub-TL's identity keys from the branch name.
        let child_identity = crate::services::agent_control::AgentIdentity::new(
            crate::services::agent_control::slugify(child_branch),
            child_agent_type,
        );
        let child_agent_name = child_identity.internal_name();
        let child_birth_branch = format!("{}.{}", ctx.birth_branch, child_agent_name);

        info!(
            child_agent = %child_agent_name,
            child_branch = %child_birth_branch,
            team = %parent_team.team_name,
            "Propagating parent team to sub-TL"
        );

        let team_info = claude_teams_bridge::TeamInfo {
            team_name: parent_team.team_name.clone(),
            inbox_name: parent_team.inbox_name.clone(),
        };

        team_reg
            .register(child_agent_name.as_str(), team_info.clone())
            .await;

        let slug = child_identity.slug();
        if slug != child_agent_name.as_str() {
            team_reg.register(slug, team_info).await;
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn enforce_harness_switch_policy(
        &self,
        ctx: &crate::effects::EffectContext,
        operation: &str,
        configured: ServiceAgentType,
        requested: AgentType,
        effective: ServiceAgentType,
        model: Option<String>,
        effort: Option<String>,
    ) -> EffectResult<()> {
        match harness_switch_decision(
            configured,
            requested,
            effective,
            harness_switch_approval_enabled(),
        ) {
            Ok(Some(policy_source)) => {
                if let Some(log) = self.ctx.event_log() {
                    let _ = log.append(
                        "agent.harness_switch",
                        ctx.agent_name.as_ref(),
                        &serde_json::json!({
                            "operation": operation,
                            "from_harness": configured.suffix(),
                            "to_harness": effective.suffix(),
                            "reason": "explicit agent_type override",
                            "approver": policy_source,
                            "policy_source": policy_source,
                            "model": model,
                            "effort": effort,
                        }),
                    );
                }
                Ok(())
            }
            Ok(None) => Ok(()),
            Err(message) => {
                if let Some(log) = self.ctx.event_log() {
                    let _ = log.append(
                        "agent.stuck",
                        ctx.agent_name.as_ref(),
                        &serde_json::json!({
                            "operation": operation,
                            "kind": "harness_switch_disallowed",
                            "configured_harness": configured.suffix(),
                            "requested_harness": effective.suffix(),
                            "guidance_required": true,
                            "model": model,
                            "effort": effort,
                            "policy": "configured_worker_harness",
                            "head_sha": null,
                            "head_sha_finding": "not_available_without_verified_pr_context",
                        }),
                    );
                }
                capture_memory(
                    ctx,
                    self.ctx.as_ref(),
                    harness_switch_stuck_capture(
                        operation,
                        configured,
                        effective,
                        model.as_deref(),
                        effort.as_deref(),
                    ),
                );
                Err(EffectError::invalid_input(message))
            }
        }
    }

    async fn ensure_tl_spawn_preflight(
        &self,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<()> {
        let mut failures = Vec::new();

        match spawn_dirty_worktree_entries(&ctx.working_dir).await {
            Ok(report) => {
                if !report.excluded.is_empty() {
                    warn!(
                        branch = %ctx.birth_branch,
                        excluded = ?report.excluded,
                        exclusions = ?report.exclusions,
                        "tracked ExoMonad runtime state is ignored by TL preflight; keep the runtime paths out of source control and track the JSON mirror where applicable"
                    );
                }
                if !report.blocking.is_empty() {
                    failures.push(dirty_worktree_message(&report.blocking));
                    failures.push(format!(
                        "TL preflight runtime exclusions (built-in plus configured): {}",
                        report.exclusions.join(", ")
                    ));
                }
            }
            Err(message) => failures.push(format!("worktree check failed: {message}")),
        }

        if failures.is_empty() {
            info!(
                branch = %ctx.birth_branch,
                working_dir = %ctx.working_dir.display(),
                "TL preflight passed before spawning"
            );
            return Ok(());
        }

        if tl_preflight_acknowledged() {
            warn!(
                branch = %ctx.birth_branch,
                failures = ?failures,
                "TL preflight failed but user acknowledgment override is set"
            );
            return Ok(());
        }

        Err(EffectError::invalid_input(format!(
            "TL preflight failed; spawning is blocked until the worktree is clean or the user acknowledges with EXOMONAD_TL_PREFLIGHT_ACK=1.\n{}",
            failures.join("\n")
        )))
    }
}

#[async_trait]
impl<
        C: HasTeamRegistry
            + HasAgentResolver
            + HasGitHubClient
            + HasProjectDir
            + HasGitWorktreeService
            + HasInboxStore
            + HasSessionMemory
            + HasSupervisorRegistry
            + HasClaudeSessionRegistry
            + HasEventLog
            + HasForgejoClient
            + HasForgejoReviewerClient
            + HasWatcherRuntimeState
            + 'static,
    > EffectHandler for AgentHandler<C>
{
    fn namespace(&self) -> &str {
        "agent"
    }

    async fn handle(
        &self,
        effect_type: &str,
        payload: &[u8],
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<Vec<u8>> {
        dispatch_agent_effect(self, effect_type, payload, ctx).await
    }
}

/// Bounded, non-empty-line summary of a repair task for the memory ledger.
/// Never captures the full task text — only its first non-empty line, capped.
fn concise_task_summary(task: &str) -> String {
    task.lines()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("")
        .trim()
        .chars()
        .take(160)
        .collect()
}

/// Build the bounded `spawned_child` memory capture for a successful spawn.
/// `branch` is empty for shared-directory workers, which have no branch.
fn spawned_child_capture(
    agent_id: &str,
    agent_type_suffix: &str,
    branch: &str,
    spawn_type: &str,
    model: Option<&str>,
    effort: Option<&str>,
    topology: &str,
) -> MemoryCapture {
    let summary = if branch.is_empty() {
        format!("Spawned {spawn_type} {agent_id} ({agent_type_suffix})")
    } else {
        format!("Spawned {spawn_type} {agent_id} on branch {branch}")
    };
    MemoryCapture {
        issue_id: None,
        kind: MemoryKind::SpawnedChild,
        importance: 60,
        summary,
        detail: None,
        metadata: Some(serde_json::json!({
            "agent_id": agent_id,
            "agent_type": agent_type_suffix,
            "branch": branch,
            "spawn_type": spawn_type,
            "model": model,
            "effort": effort,
            "topology": topology,
        })),
    }
}

/// Build the bounded `fix_direction` memory capture for a successful `resume_pr`.
#[allow(clippy::too_many_arguments)]
fn resume_fix_direction_capture(
    pr_number: u64,
    head_sha: &str,
    owner: &str,
    branch: &str,
    task: &str,
    model: Option<&str>,
    effort: Option<&str>,
    topology: &str,
) -> MemoryCapture {
    MemoryCapture {
        issue_id: None,
        kind: MemoryKind::FixDirection,
        importance: 80,
        summary: format!(
            "Resumed PR #{pr_number} (owner {owner}): {}",
            concise_task_summary(task)
        ),
        detail: None,
        metadata: Some(serde_json::json!({
            "pr_number": pr_number,
            "head_sha": head_sha,
            "owner": owner,
            "branch": branch,
            "model": model,
            "effort": effort,
            "topology": topology,
        })),
    }
}

async fn dirty_worktree_entries(project_dir: &Path) -> Result<Vec<String>, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(project_dir)
        .args(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        .output()
        .await
        .map_err(|error| format!("failed to run git status --porcelain=v1 -z: {error}"))?;

    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }

    Ok(parse_git_status_paths(&output.stdout))
}

#[derive(Debug, Default, PartialEq, Eq)]
struct SpawnPreflightReport {
    blocking: Vec<String>,
    excluded: Vec<String>,
    exclusions: Vec<String>,
}

fn runtime_path_is_excluded(path: &str, exclusions: &[String]) -> bool {
    let normalized = path.strip_prefix("./").unwrap_or(path);
    exclusions.iter().any(|rule| {
        let prefix = rule.trim_end_matches('/');
        normalized == prefix || normalized.starts_with(rule)
    })
}

fn classify_spawn_preflight_entries(
    entries: Vec<String>,
    exclusions: Vec<String>,
) -> SpawnPreflightReport {
    let mut report = SpawnPreflightReport {
        exclusions,
        ..SpawnPreflightReport::default()
    };
    for entry in entries {
        if runtime_path_is_excluded(&entry, &report.exclusions) {
            report.excluded.push(entry);
        } else {
            report.blocking.push(entry);
        }
    }
    report
}

fn parse_git_status_paths(stdout: &[u8]) -> Vec<String> {
    let records: Vec<&[u8]> = stdout
        .split(|byte| *byte == 0)
        .filter(|record| !record.is_empty())
        .collect();
    let mut paths = Vec::new();
    let mut index = 0;
    while index < records.len() {
        let record = records[index];
        if record.len() >= 4 {
            let status = &record[..2];
            paths.push(String::from_utf8_lossy(&record[3..]).into_owned());
            if status.iter().any(|byte| matches!(byte, b'R' | b'C')) {
                if let Some(previous_path) = records.get(index + 1) {
                    paths.push(String::from_utf8_lossy(previous_path).into_owned());
                    index += 1;
                }
            }
        }
        index += 1;
    }
    paths
}

async fn spawn_dirty_worktree_entries(project_dir: &Path) -> Result<SpawnPreflightReport, String> {
    let entries = dirty_worktree_entries(project_dir).await?;
    Ok(classify_spawn_preflight_entries(
        entries,
        configured_tl_preflight_runtime_paths()?,
    ))
}

struct VerifiedOrphan {
    worktree_path: PathBuf,
    agent_dir: PathBuf,
    pr_number: u64,
    pr_state: String,
}

fn cleanup_pr_state(pr: &ForgejoPullRequest) -> Result<String, String> {
    if pr.merged {
        return Ok("merged".to_string());
    }
    if pr.state.eq_ignore_ascii_case("closed") {
        return Ok("closed".to_string());
    }
    if pr.state.eq_ignore_ascii_case("open") {
        return Err(format!("PR #{} is still open", pr.number.as_u64()));
    }
    Err(format!(
        "PR #{} has unsupported state {:?}",
        pr.number.as_u64(),
        pr.state
    ))
}

fn dirty_worktree_message(entries: &[String]) -> String {
    let listed = entries
        .iter()
        .take(10)
        .map(|entry| format!("  - {entry}"))
        .collect::<Vec<_>>()
        .join("\n");
    let suffix = if entries.len() > 10 {
        format!("\n  - ... and {} more", entries.len() - 10)
    } else {
        String::new()
    };
    format!("worktree check failed: uncommitted or untracked files are present\n{listed}{suffix}")
}

fn tl_preflight_acknowledged() -> bool {
    std::env::var("EXOMONAD_TL_PREFLIGHT_ACK")
        .map(|value| matches!(value.as_str(), "1" | "true" | "yes" | "ack"))
        .unwrap_or(false)
}

fn claude_spawn_flags(
    permission_mode: String,
    allowed_tools: Vec<String>,
    disallowed_tools: Vec<String>,
) -> ClaudeSpawnFlags {
    use crate::domain::PermissionMode;
    let mode = if permission_mode.is_empty() {
        None
    } else {
        Some(
            serde_json::from_value::<PermissionMode>(serde_json::Value::String(permission_mode))
                .unwrap_or_default(),
        )
    };
    ClaudeSpawnFlags {
        permission_mode: mode,
        allowed_tools,
        disallowed_tools,
    }
}

// The retired harness keeps its historical protobuf number so old payloads can
// fail closed with the actionable deprecation message.
const RETIRED_AGENT_TYPE_VALUE: i32 = 2;

fn convert_agent_type(t: AgentType) -> EffectResult<ServiceAgentType> {
    if t as i32 == RETIRED_AGENT_TYPE_VALUE {
        return Err(EffectError::invalid_input(
            crate::services::agent_control::AGENT_TYPE_DEPRECATION_MESSAGE,
        ));
    }
    match t {
        AgentType::Claude => Ok(ServiceAgentType::Claude),
        AgentType::Shoal => Ok(ServiceAgentType::Shoal),
        AgentType::Opencode => Ok(ServiceAgentType::OpenCode),
        AgentType::Codex => Ok(ServiceAgentType::Codex),
        AgentType::Unspecified => Err(EffectError::invalid_input(
            "agent_type is required (must be 'claude', 'shoal', 'opencode', or 'codex', got UNSPECIFIED)",
        )),
        _ => Err(EffectError::invalid_input(
            crate::services::agent_control::AGENT_TYPE_DEPRECATION_MESSAGE,
        )),
    }
}

fn convert_agent_type_or_default(
    t: AgentType,
    default_type: ServiceAgentType,
) -> EffectResult<ServiceAgentType> {
    if t as i32 == RETIRED_AGENT_TYPE_VALUE {
        return Err(EffectError::invalid_input(
            crate::services::agent_control::AGENT_TYPE_DEPRECATION_MESSAGE,
        ));
    }
    match t {
        AgentType::Unspecified => Ok(default_type),
        _ => convert_agent_type(t),
    }
}

fn proto_agent_type_label(t: AgentType) -> &'static str {
    if t as i32 == RETIRED_AGENT_TYPE_VALUE {
        return "retired";
    }
    match t {
        AgentType::Claude => "claude",
        AgentType::Shoal => "shoal",
        AgentType::Opencode => "opencode",
        AgentType::Codex => "codex",
        AgentType::Unspecified => "unspecified",
        _ => "retired",
    }
}

const HARNESS_SWITCH_APPROVAL_ENV: &str = "EXOMONAD_ALLOW_HARNESS_SWITCH";

fn harness_switch_approval_enabled() -> bool {
    std::env::var(HARNESS_SWITCH_APPROVAL_ENV)
        .map(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes"
            )
        })
        .unwrap_or(false)
}

fn harness_switch_decision(
    configured: ServiceAgentType,
    requested: AgentType,
    effective: ServiceAgentType,
    approved: bool,
) -> Result<Option<&'static str>, String> {
    if requested == AgentType::Unspecified || effective == configured {
        return Ok(None);
    }
    if approved {
        return Ok(Some(HARNESS_SWITCH_APPROVAL_ENV));
    }
    Err(format!(
        "[STUCK: harness-switch] configured worker harness '{}' cannot be replaced by '{}' for this {}. Retry with the configured harness or obtain explicit human approval via {}=1; no automatic cross-harness fallback is allowed.",
        configured.suffix(),
        effective.suffix(),
        "coding assignment",
        HARNESS_SWITCH_APPROVAL_ENV,
    ))
}

fn harness_switch_stuck_capture(
    operation: &str,
    configured: ServiceAgentType,
    requested: ServiceAgentType,
    model: Option<&str>,
    effort: Option<&str>,
) -> MemoryCapture {
    MemoryCapture {
        issue_id: None,
        kind: MemoryKind::Blocker,
        importance: 90,
        summary: format!(
            "[STUCK: harness-switch] {operation} kept configured {} instead of switching to {}",
            configured.suffix(),
            requested.suffix()
        ),
        detail: None,
        metadata: Some(serde_json::json!({
            "operation": operation,
            "configured_harness": configured.suffix(),
            "requested_harness": requested.suffix(),
            "guidance_required": true,
            "model": model,
            "effort": effort,
            "policy": "configured_worker_harness",
        })),
    }
}

fn parse_issue_number(issue: &str) -> EffectResult<IssueNumber> {
    let n: u64 = issue
        .parse()
        .map_err(|_| EffectError::invalid_input(format!("Invalid issue number: {}", issue)))?;
    IssueNumber::try_from(n).map_err(|e| EffectError::invalid_input(e.to_string()))
}

fn parse_owner(owner: &str) -> EffectResult<GithubOwner> {
    GithubOwner::try_from(owner.to_string()).map_err(|e| EffectError::invalid_input(e.to_string()))
}

fn parse_repo(repo: &str) -> EffectResult<GithubRepo> {
    GithubRepo::try_from(repo.to_string()).map_err(|e| EffectError::invalid_input(e.to_string()))
}

fn watcher_pr_state_error(pr_number: u64, error: impl Into<String>) -> WatcherPrStateResponse {
    let error = error.into();
    WatcherPrStateResponse {
        success: false,
        error: error.clone(),
        pr_number,
        found: false,
        review_state: "unknown".to_string(),
        ci_status: CIStatus::Unknown.as_str().to_string(),
        head_sha: String::new(),
        head_branch: String::new(),
        base_branch: String::new(),
        base_sha: String::new(),
        patch_digest: String::new(),
        merge_tree_sha: String::new(),
        pr_state: String::new(),
        merged: false,
        review_count: 0,
        head_reachable: false,
        evidence_error: error,
        publication_ownership_verified: false,
        publication_ownership_error: String::new(),
        publication: None,
        review_id: 0,
        review_verdict: String::new(),
        review_head_sha: String::new(),
        reviewer_agent_id: String::new(),
        reviewer_identity_error: String::new(),
        review_body: String::new(),
    }
}

fn published_head_evidence(publication: Option<&PublishedHead>) -> Option<PublishedHeadEvidence> {
    publication.map(|head| PublishedHeadEvidence {
        invocation_id: head.invocation_id.clone().unwrap_or_default(),
        slice_id: head.slice_id.clone().unwrap_or_default(),
        author_agent: head.author_agent.clone().unwrap_or_default(),
        succession_invocation_ids: head
            .invocation_succession
            .iter()
            .map(|succession| succession.to_invocation_id.clone())
            .collect(),
    })
}

pub(crate) async fn publication_ownership_status<C>(
    ctx: &C,
    pr_number: u64,
    head_branch: &str,
    base_branch: &str,
    head_sha: &str,
) -> (bool, String)
where
    C: HasAgentResolver + HasProjectDir,
{
    match verify_current_publication_ownership(
        ctx.project_dir(),
        ctx.agent_resolver(),
        pr_number,
        head_branch,
        base_branch,
        head_sha,
    )
    .await
    {
        Ok(()) => (true, String::new()),
        Err(error) => (false, error.to_string()),
    }
}

fn review_state_from_forgejo_reviews(
    reviews: &[ForgejoPullRequestReview],
    head_sha: &str,
) -> (String, u32) {
    let review_count = reviews
        .iter()
        .filter(|review| review_matches_exact_head(review, head_sha))
        .count() as u32;
    let state = latest_exact_forgejo_review(reviews, head_sha)
        .and_then(|review| normalized_review_verdict(&review.state))
        .unwrap_or("pending_review");
    (state.to_string(), review_count)
}

fn review_matches_exact_head(review: &ForgejoPullRequestReview, head_sha: &str) -> bool {
    !head_sha.is_empty()
        && !review.dismissed
        && !review.stale
        && review.commit_id.as_deref() == Some(head_sha)
        && normalized_review_verdict(&review.state).is_some()
}

fn latest_exact_forgejo_review<'a>(
    reviews: &'a [ForgejoPullRequestReview],
    head_sha: &str,
) -> Option<&'a ForgejoPullRequestReview> {
    reviews
        .iter()
        .rev()
        .find(|review| review_matches_exact_head(review, head_sha))
}

fn normalized_review_verdict(state: &str) -> Option<&'static str> {
    normalize_review_verdict(state).map(|verdict| verdict.as_str())
}

pub(crate) fn review_author_matches_reviewer_login(
    review_login: Option<&str>,
    reviewer_login: Option<&str>,
) -> bool {
    let Some(review_login) = review_login
        .map(str::trim)
        .filter(|value| !value.is_empty())
    else {
        return false;
    };
    let Some(reviewer_login) = reviewer_login
        .map(str::trim)
        .filter(|value| !value.is_empty())
    else {
        return false;
    };
    review_login.eq_ignore_ascii_case(reviewer_login)
}

async fn resolve_durable_review_author<C: HasAgentResolver>(
    ctx: &C,
    pr_number: u64,
    head_sha: &str,
    login: Option<&str>,
    reviewer_login: Option<&str>,
    owner_agent: Option<&str>,
) -> Option<String> {
    if !review_author_matches_reviewer_login(login, reviewer_login) {
        return None;
    }
    let resolved = ctx
        .agent_resolver()
        .resolve_reviewer_invocation(pr_number, head_sha)
        .await?;
    if owner_agent == Some(resolved.as_str()) {
        None
    } else {
        Some(resolved)
    }
}

#[async_trait]
impl<
        C: HasTeamRegistry
            + HasAgentResolver
            + HasGitHubClient
            + HasProjectDir
            + HasGitWorktreeService
            + HasInboxStore
            + HasSessionMemory
            + HasSupervisorRegistry
            + HasClaudeSessionRegistry
            + HasEventLog
            + HasForgejoClient
            + HasForgejoReviewerClient
            + HasWatcherRuntimeState
            + 'static,
    > AgentEffects for AgentHandler<C>
{
    async fn spawn(
        &self,
        req: SpawnRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnResponse> {
        self.ensure_tl_spawn_preflight(ctx).await?;
        let issue_number = parse_issue_number(&req.issue)?;
        let requested_agent_type = req.agent_type();
        let effective_agent_type = convert_agent_type(requested_agent_type)?;
        self.enforce_harness_switch_policy(
            ctx,
            "spawn",
            self.service.default_spawn_agent_type(),
            requested_agent_type,
            effective_agent_type,
            self.service
                .effective_model_for(effective_agent_type, "tl", None),
            self.service.effective_effort_for("tl", None),
        )?;
        let options = SpawnOptions {
            owner: parse_owner(&req.owner)?,
            repo: parse_repo(&req.repo)?,
            agent_type: effective_agent_type,
            subrepo: non_empty(req.subrepo).map(PathBuf::from),
            base_branch: non_empty(req.base_branch).map(|s| {
                BirthBranch::try_from_str(s.as_str()).expect("validated string input is non-empty")
            }),
        };

        let result = self
            .service
            .spawn_agent(issue_number, &options, &ctx.birth_branch)
            .await
            .effect_err_preserve("agent")?;

        Ok(SpawnResponse {
            agent: Some(spawn_result_to_proto(&req.issue, &result)),
        })
    }

    async fn spawn_batch(
        &self,
        req: SpawnBatchRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnBatchResponse> {
        self.ensure_tl_spawn_preflight(ctx).await?;
        let requested_agent_type = req.agent_type();
        let agent_type = convert_agent_type(requested_agent_type)?;
        self.enforce_harness_switch_policy(
            ctx,
            "spawn_batch",
            self.service.default_spawn_agent_type(),
            requested_agent_type,
            agent_type,
            self.service.effective_model_for(agent_type, "tl", None),
            self.service.effective_effort_for("tl", None),
        )?;
        let mut agents = Vec::new();
        let mut errors = Vec::new();

        for issue in &req.issues {
            let issue_number = match parse_issue_number(issue) {
                Ok(n) => n,
                Err(e) => {
                    errors.push(format!("Issue {}: {}", issue, e));
                    continue;
                }
            };
            let options = SpawnOptions {
                owner: parse_owner(&req.owner)?,
                repo: parse_repo(&req.repo)?,
                agent_type,
                subrepo: non_empty(req.subrepo.clone()).map(PathBuf::from),
                base_branch: None,
            };

            match self
                .service
                .spawn_agent(issue_number, &options, &ctx.birth_branch)
                .await
            {
                Ok(result) => agents.push(spawn_result_to_proto(issue, &result)),
                Err(e) => errors.push(format!("Issue {}: {}", issue, e)),
            }
        }

        Ok(SpawnBatchResponse { agents, errors })
    }

    async fn spawn_worker(
        &self,
        req: SpawnWorkerRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnWorkerResponse> {
        self.ensure_tl_spawn_preflight(ctx).await?;
        let default_type = self.service.default_spawn_agent_type();
        let requested_agent_type = req.agent_type();
        let effective_agent_type =
            convert_agent_type_or_default(requested_agent_type, default_type)?;
        let requested_model = non_empty(req.model.clone());
        let model = self.service.effective_model_for(
            effective_agent_type,
            "worker",
            requested_model.as_deref(),
        );
        let effort = self.service.effective_effort_for("worker", None);
        self.enforce_harness_switch_policy(
            ctx,
            "spawn_worker",
            default_type,
            requested_agent_type,
            effective_agent_type,
            model.clone(),
            effort.clone(),
        )?;
        info!(
            requested_agent_type = proto_agent_type_label(requested_agent_type),
            default_agent_type = default_type.suffix(),
            effective_agent_type = effective_agent_type.suffix(),
            name = %req.name,
            "Resolved spawn_worker agent type"
        );
        let options = SpawnWorkerOptions {
            name: AgentName::try_from_str(req.name.as_str())
                .expect("validated string input is non-empty"),
            prompt: req.prompt.clone(),
            agent_type: effective_agent_type,
            model: requested_model,
            claude_flags: claude_spawn_flags(
                req.permission_mode.clone(),
                req.allowed_tools.clone(),
                req.disallowed_tools.clone(),
            ),
        };

        let result = match self
            .service
            .spawn_worker_with_intent(&options, ctx, Some(req.intent_id.as_str()))
            .await
            .effect_err_preserve("agent")
        {
            Ok(result) => result,
            Err(error) => {
                append_spawn_failed(
                    &self.ctx,
                    ctx.agent_name.as_ref(),
                    &req.name,
                    &req.intent_id,
                    &error.to_string(),
                );
                return Err(error);
            }
        };
        let agent_info = worker_result_to_proto(&req.name, &result);
        let provenance = self.ctx.agent_resolver().get(&result.agent_name).await;
        let model = provenance
            .as_ref()
            .and_then(|record| record.model.clone())
            .or(model);
        let effort = provenance
            .as_ref()
            .and_then(|record| record.effort.clone())
            .or(effort);
        let topology = provenance
            .as_ref()
            .map(|record| format!("{:?}", record.topology).to_lowercase())
            .unwrap_or_else(|| "unknown".to_string());

        tracing::info!(
            otel.name = "agent.spawned",
            child_agent = %agent_info.id,
            agent_type = %AgentType::try_from(agent_info.agent_type).map(|t| format!("{:?}", t)).unwrap_or_else(|_| "unknown".to_string()),
            branch = %agent_info.branch_name,
            spawn_type = "worker",
            model = ?model,
            effort = ?effort,
            topology = %topology,
            "[event] agent.spawned"
        );
        if let Some(log) = self.ctx.event_log() {
            let mut payload = serde_json::json!({
                "child_agent": agent_info.id,
                "agent_type": format!("{:?}", options.agent_type).to_lowercase(),
                "spawn_type": "worker",
                "branch": agent_info.branch_name,
                "model": model,
                "effort": effort,
                "topology": topology,
            });
            if !req.intent_id.trim().is_empty() {
                payload["intent_id"] = serde_json::json!(req.intent_id);
            }
            let _ = log.append("agent.spawned", ctx.agent_name.as_ref(), &payload);
        }

        if options.agent_type == ServiceAgentType::Claude {
            // Claude Code workers can participate in Claude Teams inboxes.
            let identity = crate::services::agent_control::AgentIdentity::new(
                crate::services::agent_control::slugify(&req.name),
                options.agent_type,
            );
            self.register_claude_team_child(
                &identity.internal_name(),
                options.agent_type.suffix(),
                &req.name,
                ctx,
            )
            .await;
        } else {
            self.register_child_supervisor(agent_info.id.as_str(), ctx)
                .await;
        }

        capture_memory(
            ctx,
            self.ctx.as_ref(),
            spawned_child_capture(
                &agent_info.id,
                options.agent_type.suffix(),
                "",
                "worker",
                model.as_deref(),
                effort.as_deref(),
                &topology,
            ),
        );

        Ok(SpawnWorkerResponse {
            agent: Some(agent_info),
        })
    }

    async fn spawn_subtree(
        &self,
        req: SpawnSubtreeRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnSubtreeResponse> {
        self.ensure_tl_spawn_preflight(ctx).await?;
        // Only look up session for --fork-session when explicitly requested.
        // Default (fork_session=false) starts the child fresh — avoids stale/compacted
        // session IDs causing "No conversation found" errors.
        let parent_session_id = if req.fork_session {
            let key = if ctx.agent_name.as_str().is_empty() {
                crate::domain::AgentName::try_from_str("root")
                    .expect("literal validated string is non-empty")
            } else {
                ctx.agent_name.clone()
            };
            let claude_uuid = self.ctx.claude_session_registry().get(key.as_str()).await;
            info!(
                key = %key,
                claude_uuid = ?claude_uuid,
                "Looked up Claude session UUID for spawn_subtree (fork_session=true)"
            );
            if claude_uuid.is_none() {
                warn!(
                    key = %key,
                    "No Claude session UUID registered — child will start without --fork-session context. Ensure SessionStart hook is configured."
                );
            }
            claude_uuid.map(|s| {
                ClaudeSessionUuid::try_from_str(s.as_str())
                    .expect("validated string input is non-empty")
            })
        } else {
            info!("fork_session=false, child starts fresh");
            None
        };

        let default_type = self.service.default_spawn_agent_type();
        let requested_agent_type = req.agent_type();
        let effective_agent_type =
            convert_agent_type_or_default(requested_agent_type, default_type)?;
        let role_name = if req.role.trim().is_empty() {
            "tl"
        } else {
            req.role.as_str()
        };
        let model = self
            .service
            .effective_model_for(effective_agent_type, role_name, None);
        let effort = self.service.effective_effort_for(role_name, None);
        self.enforce_harness_switch_policy(
            ctx,
            "spawn_subtree",
            default_type,
            requested_agent_type,
            effective_agent_type,
            model.clone(),
            effort.clone(),
        )?;
        info!(
            requested_agent_type = proto_agent_type_label(requested_agent_type),
            default_agent_type = default_type.suffix(),
            effective_agent_type = effective_agent_type.suffix(),
            branch_name = %req.branch_name,
            "Resolved spawn_subtree agent type"
        );
        let options = SpawnSubtreeOptions {
            task: req.task.clone(),
            branch_name: req.branch_name.clone(),
            parent_session_id,
            role: non_empty(req.role.clone()).map(crate::domain::Role::new),
            agent_type: effective_agent_type,
            claude_flags: claude_spawn_flags(
                req.permission_mode.clone(),
                req.allowed_tools.clone(),
                req.disallowed_tools.clone(),
            ),
            working_dir: non_empty(req.working_dir).map(PathBuf::from),
            permissions: req.permissions.map(|p| AgentPermissions {
                allow: p.allow,
                deny: p.deny,
                default_mode: None,
            }),
            standalone_repo: req.standalone_repo,
            allowed_dirs: req.allowed_dirs,
            model: None,
            effort: None,
            invocation_pr_number: None,
            invocation_head_sha: None,
        };

        let result = self
            .service
            .spawn_subtree(&options, &ctx.birth_branch)
            .await
            .effect_err_preserve("agent")?;

        let agent_info = subtree_result_to_proto(&req.branch_name, &result)?;
        let provenance = self.ctx.agent_resolver().get(&result.agent_name).await;
        let model = provenance
            .as_ref()
            .and_then(|record| record.model.clone())
            .or(model);
        let effort = provenance
            .as_ref()
            .and_then(|record| record.effort.clone())
            .or(effort);
        let topology = provenance
            .as_ref()
            .map(|record| format!("{:?}", record.topology).to_lowercase())
            .unwrap_or_else(|| "unknown".to_string());

        tracing::info!(
            otel.name = "agent.spawned",
            child_agent = %agent_info.id,
            agent_type = %AgentType::try_from(agent_info.agent_type).map(|t| format!("{:?}", t)).unwrap_or_else(|_| "unknown".to_string()),
            branch = %agent_info.branch_name,
            spawn_type = "subtree",
            model = ?model,
            effort = ?effort,
            topology = %topology,
            "[event] agent.spawned"
        );
        if let Some(log) = self.ctx.event_log() {
            let _ = log.append(
                "agent.spawned",
                ctx.agent_name.as_ref(),
                &serde_json::json!({
                    "child_agent": agent_info.id, "agent_type": format!("{:?}", options.agent_type), "spawn_type": "subtree",
                    "branch": agent_info.branch_name,
                    "model": model,
                    "effort": effort,
                    "topology": topology,
                }),
            );
        }

        capture_memory(
            ctx,
            self.ctx.as_ref(),
            spawned_child_capture(
                &agent_info.id,
                options.agent_type.suffix(),
                &agent_info.branch_name,
                "subtree",
                model.as_deref(),
                effort.as_deref(),
                &topology,
            ),
        );

        if options.agent_type == ServiceAgentType::Claude {
            let child_identity = crate::services::agent_control::AgentIdentity::new(
                crate::services::agent_control::slugify(&req.branch_name),
                options.agent_type,
            );
            let member_type_suffix = options.agent_type.suffix();
            self.register_claude_team_child(
                &child_identity.internal_name(),
                &format!("{}-subtree", member_type_suffix),
                &req.branch_name,
                ctx,
            )
            .await;

            // Propagate parent's team to sub-TL's identity keys so the sub-TL can
            // register its own Claude Code workers as synthetic members.
            self.propagate_team_to_child(&req.branch_name, options.agent_type, ctx)
                .await;
        } else {
            self.register_child_supervisor(agent_info.id.as_str(), ctx)
                .await;
        }

        Ok(SpawnSubtreeResponse {
            agent: Some(agent_info),
        })
    }

    async fn spawn_reviewer(
        &self,
        req: SpawnReviewerRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnReviewerResponse> {
        if req.pr_number == 0 {
            return Err(EffectError::invalid_input("pr_number is required"));
        }

        let pr = self.resolve_open_forgejo_pr_entry(req.pr_number).await?;
        let requested_head_sha = req.head_sha.trim();
        if requested_head_sha.is_empty() {
            return Err(EffectError::invalid_input("head_sha is required"));
        }
        if req.acceptance_criteria.is_empty()
            || req
                .acceptance_criteria
                .iter()
                .any(|criterion| criterion.trim().is_empty())
        {
            return Err(EffectError::invalid_input(
                "acceptance_criteria must contain only non-empty items",
            ));
        }
        let live_head_sha = pr
            .last_head_sha
            .as_deref()
            .filter(|sha| !sha.trim().is_empty());
        if live_head_sha != Some(requested_head_sha) {
            return Err(EffectError::invalid_input(format!(
                "reviewer head SHA mismatch: requested {requested_head_sha}, live PR head is {}",
                live_head_sha.unwrap_or("unavailable"),
            )));
        }
        let active_reviewer = live_reviewer_for_pr(&self.service, req.pr_number).await;
        if !req.force {
            if let Some(reviewer_name) = active_reviewer.as_ref() {
                return Ok(SpawnReviewerResponse {
                    agent: None,
                    reviewer_name: reviewer_name.clone(),
                    already_active: true,
                });
            }
        }

        clear_reviewer_review_artifacts(self.ctx.project_dir(), req.pr_number)
            .await
            .effect_err("agent")?;
        if req.force {
            cleanup_force_reviewer_resources(&self.service, req.pr_number).await;
        }

        let reviewer_branch = if req.force {
            format!("review-pr-{}-{}", pr.number, Utc::now().timestamp_millis())
        } else {
            format!("review-pr-{}", pr.number)
        };
        let result = self
            .service
            .spawn_reviewer_for_recovery_with_criteria_named(
                &pr,
                &ctx.birth_branch,
                &reviewer_branch,
                &req.acceptance_criteria,
            )
            .await
            .effect_err_preserve("agent")?;
        let agent_info = subtree_result_to_proto(&reviewer_branch, &result)?;
        let reviewer_name = result.agent_name.to_string();

        if result.agent_type == ServiceAgentType::Claude {
            self.register_claude_team_child(
                &result.agent_name,
                &format!("{}-reviewer", result.agent_type.suffix()),
                &reviewer_branch,
                ctx,
            )
            .await;
        } else {
            self.register_child_supervisor(agent_info.id.as_str(), ctx)
                .await;
        }

        Ok(SpawnReviewerResponse {
            agent: Some(agent_info),
            reviewer_name,
            already_active: false,
        })
    }

    async fn cleanup_reviewer_leaf(
        &self,
        req: CleanupReviewerLeafRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<CleanupReviewerLeafResponse> {
        if req.pr_number == 0 {
            return Err(EffectError::invalid_input("pr_number is required"));
        }

        let cleaned_reviewers =
            cleanup_force_reviewer_resources(&self.service, req.pr_number).await;
        Ok(cleanup_reviewer_leaf_response(
            req.pr_number,
            cleaned_reviewers,
            clear_reviewer_review_artifacts(self.ctx.project_dir(), req.pr_number).await,
        ))
    }

    async fn restart_review(
        &self,
        req: RestartReviewRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<RestartReviewResponse> {
        if req.pr_number == 0 {
            return Err(EffectError::invalid_input("pr_number is required"));
        }

        let pr = self.resolve_open_forgejo_pr_entry(req.pr_number).await?;
        tracing::info!(
            pr_number = req.pr_number,
            head_branch = %pr.head_branch,
            head_sha = ?pr.last_head_sha,
            "Resetting same-PR review cycle"
        );

        let cleaned_reviewers =
            cleanup_force_reviewer_resources(&self.service, req.pr_number).await;
        let runtime_state_found = self
            .ctx
            .watcher_runtime_state()
            .reset_review_cycle(req.pr_number)
            .await;

        match reset_reviewer_restart_artifacts(self.ctx.project_dir(), req.pr_number).await {
            Ok(reset) => Ok(RestartReviewResponse {
                success: true,
                error: String::new(),
                pr_number: req.pr_number,
                cleaned_reviewers,
                runtime_state_found,
                watcher_state_found: reset.watcher_state_found,
                legacy_review_file_removed: reset.legacy_review_file_removed,
            }),
            Err(error) => Ok(RestartReviewResponse {
                success: false,
                error: error.to_string(),
                pr_number: req.pr_number,
                cleaned_reviewers,
                runtime_state_found,
                watcher_state_found: false,
                legacy_review_file_removed: false,
            }),
        }
    }

    async fn replace_closed_pr(
        &self,
        req: ReplaceClosedPrRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<ReplaceClosedPrResponse> {
        let issue_id = req.chainlink_issue_id;
        let old_pr_number = req.closed_pr_number;
        let old_leaf_name = req.old_leaf_name.trim().to_string();
        let new_leaf_name = req.new_leaf_name.trim().to_string();
        let replacement_task = req.replacement_task.trim().to_string();

        if issue_id == 0 {
            return Ok(replace_closed_pr_error("chainlink_issue_id is required"));
        }
        if old_pr_number == 0 {
            return Ok(replace_closed_pr_error("closed_pr_number is required"));
        }
        if old_leaf_name.is_empty() {
            return Ok(replace_closed_pr_error("old_leaf_name is required"));
        }
        if new_leaf_name.is_empty() {
            return Ok(replace_closed_pr_error("new_leaf_name is required"));
        }
        if replacement_task.is_empty() {
            return Ok(replace_closed_pr_error("replacement_task is required"));
        }
        if !req.human_approved {
            return Ok(replace_closed_pr_error(
                "human_approved must be true before retiring the old PR and leaf",
            ));
        }

        let new_slug = slugify(&new_leaf_name);
        if new_slug != new_leaf_name {
            return Ok(replace_closed_pr_error(
                "new_leaf_name must be a fresh bare slug without a runtime suffix",
            ));
        }
        let old_identity = AgentIdentity::from_internal_name(&old_leaf_name);
        if new_slug == old_identity.slug() {
            return Ok(replace_closed_pr_error(
                "new_leaf_name must differ from the old leaf identity",
            ));
        }

        if let Err(error) = ensure_chainlink_issue_open(self.ctx.project_dir(), issue_id).await {
            return Ok(replace_closed_pr_error(&error));
        }

        let pr = match self.resolve_forgejo_pr(old_pr_number).await {
            Ok(pr) => pr,
            Err(error) => return Ok(replace_closed_pr_error(&error.to_string())),
        };
        if let Err(error) = ensure_replaceable_unmerged_pr(&pr, old_pr_number) {
            return Ok(replace_closed_pr_error(&error.to_string()));
        }

        let metadata = parse_pr_body_metadata(&pr.body);
        if let Some(metadata_issue_id) = metadata.chainlink_issue_id {
            if metadata_issue_id != issue_id {
                return Ok(replace_closed_pr_error(&format!(
                    "PR #{old_pr_number} belongs to Chainlink issue #{metadata_issue_id}, not #{issue_id}"
                )));
            }
        }
        if let Some(author_agent) = metadata.author_agent.as_deref() {
            if !leaf_identity_matches(author_agent, &old_leaf_name) {
                return Ok(replace_closed_pr_error(&format!(
                    "PR #{old_pr_number} author '{author_agent}' does not match old_leaf_name '{old_leaf_name}'"
                )));
            }
        }

        let source_head_sha = match pr.head_sha.clone().filter(|sha| !sha.trim().is_empty()) {
            Some(sha) => sha,
            None => {
                return Ok(replace_closed_pr_error(&format!(
                    "PR #{old_pr_number} has no exact head SHA; refusing replacement"
                )));
            }
        };
        let old_head_branch = pr.head_ref.to_string();
        let original_base_branch = pr.base_ref.to_string();
        if let Err(error) = ensure_git_revision_reachable(
            self.ctx.project_dir(),
            &old_head_branch,
            &source_head_sha,
        )
        .await
        {
            return Ok(replace_closed_pr_error(&error));
        }

        let agent_type = convert_agent_type_or_default(
            req.agent_type(),
            self.service.default_spawn_agent_type(),
        )?;
        let new_identity = AgentIdentity::new(new_slug.clone(), agent_type);
        let new_internal_name = new_identity.internal_name().to_string();
        let new_branch = match BirthBranch::try_from_str(&original_base_branch) {
            Ok(base) => base.child(&new_internal_name).to_string(),
            Err(error) => return Ok(replace_closed_pr_error(&error.to_string())),
        };
        let new_worktree_path = self.service.worktree_base.join(new_internal_name.as_str());
        let record_path = replacement_record_path(self.ctx.project_dir(), old_pr_number);

        if let Some(record) = read_replacement_record(&record_path).await? {
            if !record.matches_request(
                issue_id,
                old_pr_number,
                &old_leaf_name,
                &new_slug,
                &source_head_sha,
                &original_base_branch,
            ) {
                return Ok(replace_closed_pr_error(&format!(
                    "replacement record for PR #{old_pr_number} does not match this request"
                )));
            }
            if record.spawn_status == "spawned" {
                return Ok(record.to_response(true));
            }
        } else if let Some(conflict) = self
            .replacement_identity_conflict(&new_slug, &new_branch, &new_worktree_path)
            .await?
        {
            return Ok(replace_closed_pr_error(&conflict));
        }

        let mut record = ReplacementRecord::new(
            issue_id,
            old_pr_number,
            &pr.state,
            pr.merged,
            &old_head_branch,
            &source_head_sha,
            &original_base_branch,
            &old_leaf_name,
            &new_slug,
            &new_branch,
            &new_worktree_path,
        );
        write_replacement_record(&record_path, &record).await?;

        let cleaned_reviewers =
            cleanup_force_reviewer_resources(&self.service, old_pr_number).await;
        let mut retired_resources = cleaned_reviewers
            .iter()
            .map(|name| format!("reviewer:{name}"))
            .collect::<Vec<_>>();
        if let Err(error) =
            clear_reviewer_review_artifacts(self.ctx.project_dir(), old_pr_number).await
        {
            record.spawn_status = "cleanup_failed".to_string();
            record.error = error.to_string();
            record.retired_resources = retired_resources;
            write_replacement_record(&record_path, &record).await?;
            return Ok(record.to_response(false));
        }
        retired_resources.push(format!("watcher-state:pr-{old_pr_number}"));

        if let Err(error) = self.service.cleanup_agent(&old_leaf_name).await {
            record.spawn_status = "cleanup_failed".to_string();
            record.error = error.to_string();
            record.retired_resources = retired_resources;
            write_replacement_record(&record_path, &record).await?;
            return Ok(record.to_response(false));
        }
        if old_leaf_worktree_exists(&self.service.worktree_base, &old_identity) {
            record.spawn_status = "cleanup_failed".to_string();
            record.error = format!(
                "old leaf worktree for '{}' still exists after cleanup",
                old_leaf_name
            );
            record.retired_resources = retired_resources;
            write_replacement_record(&record_path, &record).await?;
            return Ok(record.to_response(false));
        }
        retired_resources.push(format!("author-worktree:{old_leaf_name}"));
        record.retired_resources = retired_resources;
        record.spawn_status = "cleanup_complete".to_string();
        write_replacement_record(&record_path, &record).await?;

        let operator_context = req.operator_context.trim();
        let old_pr_context = if pr.state.eq_ignore_ascii_case("open") {
            "open and unmerged; this tool does not close it. After verifying the replacement PR, explicitly reconcile or close the old PR"
        } else {
            "closed and unmerged; do not reopen or resume it"
        };
        let task = format!(
            "{replacement_task}\n\nReplacement PR context:\n- Chainlink issue: #{issue_id} (keep this issue open and continue it)\n- Old PR: #{old_pr_number} ({old_pr_context})\n- Source branch: {old_head_branch}\n- Source head SHA: {source_head_sha}\n- New PR base branch: {original_base_branch}\n- Fresh leaf identity: {new_leaf_name}\n\nStart from the exact source SHA, make the requested fixes, and file a NEW PR targeting the stated base branch. Do not create a new Chainlink issue.{}",
            if operator_context.is_empty() {
                String::new()
            } else {
                format!("\n- Operator context: {operator_context}")
            }
        );
        let options = SpawnLeafOptions {
            task,
            branch_name: new_slug.clone(),
            role: Some(crate::domain::Role::dev()),
            agent_type,
            claude_flags: ClaudeSpawnFlags::default(),
            standalone_repo: false,
            allowed_dirs: Vec::new(),
            start_point: Some(source_head_sha.clone()),
            base_branch: Some(original_base_branch.clone()),
            expected_agent_name: None,
            invocation_pr_number: None,
            recovery_lineage: None,
            model: None,
        };
        info!(
            chainlink_issue_id = issue_id,
            old_pr_number,
            old_pr_state = %pr.state,
            source_head_sha = %source_head_sha,
            original_base_branch = %original_base_branch,
            new_branch = %new_branch,
            "Spawning approved PR replacement leaf"
        );
        let spawn_result = match self
            .service
            .spawn_leaf_subtree(&options, &ctx.birth_branch)
            .await
        {
            Ok(result) => result,
            Err(error) => {
                warn!(
                    chainlink_issue_id = issue_id,
                    old_pr_number,
                    source_head_sha = %source_head_sha,
                    original_base_branch = %original_base_branch,
                    new_branch = %new_branch,
                    error = %error,
                    "Approved PR replacement leaf failed to spawn"
                );
                record.spawn_status = "spawn_failed".to_string();
                record.error = error.to_string();
                write_replacement_record(&record_path, &record).await?;
                return Ok(record.to_response(false));
            }
        };

        let agent_info = leaf_subtree_result_to_proto(&new_slug, &spawn_result)?;
        if spawn_result.agent_type == ServiceAgentType::Claude {
            self.register_claude_team_child(
                &spawn_result.agent_name,
                &format!("{}-leaf", spawn_result.agent_type.suffix()),
                &new_branch,
                ctx,
            )
            .await;
        } else {
            self.register_child_supervisor(agent_info.id.as_str(), ctx)
                .await;
        }

        record.spawn_status = "spawned".to_string();
        record.error.clear();
        record.worktree_path = spawn_result.worktree_path.to_string_lossy().to_string();
        info!(
            chainlink_issue_id = issue_id,
            old_pr_number,
            old_pr_state = %pr.state,
            source_head_sha = %source_head_sha,
            original_base_branch = %original_base_branch,
            new_branch = %new_branch,
            new_agent = %agent_info.id,
            "Approved PR replacement leaf spawned; new PR will be filed by the leaf"
        );
        write_replacement_record(&record_path, &record).await?;
        if let Some(log) = self.ctx.event_log() {
            let _ = log.append(
                "pr.replaced",
                ctx.agent_name.as_ref(),
                &serde_json::json!({
                    "chainlink_issue_id": issue_id,
                    "old_pr_number": old_pr_number,
                    "old_leaf_name": old_leaf_name,
                    "source_branch": old_head_branch,
                    "source_head_sha": source_head_sha,
                    "base_branch": original_base_branch,
                    "new_leaf_name": new_slug,
                    "new_branch": new_branch,
                    "worktree_path": record.worktree_path,
                    "new_agent": agent_info.id,
                }),
            );
        }
        Ok(record.to_response(false))
    }

    async fn resolve_live_pr_for_slice(
        &self,
        req: ResolveLivePrForSliceRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<ResolveLivePrForSliceResponse> {
        let slice_id = req.slice_id.trim();
        if slice_id.is_empty() {
            return Err(EffectError::invalid_input("slice_id is required"));
        }
        let heads = read_published_heads(self.ctx.project_dir())
            .await
            .effect_err("agent")?;
        let abandoned_attempts = if let Some(event_log) = self.ctx.event_log() {
            event_log
                .ledger()
                .read_events()
                .effect_err("agent")?
                .into_iter()
                .filter_map(|record| {
                    if record.event.event_type != "tl.slice_abandoned" {
                        return None;
                    }
                    let data = record.event.data.as_object()?;
                    if data.get("slice_id").and_then(|value| value.as_str()) != Some(slice_id) {
                        return None;
                    }
                    Some(AbandonedAttempt {
                        attempt: data
                            .get("attempt")
                            .and_then(|value| value.as_u64())
                            .unwrap_or(1),
                        pr_number: data
                            .get("pr_number")
                            .and_then(|value| value.as_u64())
                            .filter(|value| *value > 0),
                        head_sha: data
                            .get("head_sha")
                            .and_then(|value| value.as_str())
                            .map(str::to_string),
                        invocation_id: data
                            .get("invocation_id")
                            .and_then(|value| value.as_str())
                            .filter(|value| !value.is_empty())
                            .map(str::to_string),
                    })
                })
                .collect::<Vec<_>>()
        } else {
            Vec::new()
        };
        let Some(forgejo) = self.ctx.forgejo_client() else {
            return Ok(ResolveLivePrForSliceResponse {
                success: false,
                error: "Forgejo is not configured; cannot resolve a live PR".to_string(),
                slice_id: slice_id.to_string(),
                resolution: LivePrResolutionKind::Unspecified as i32,
                pr_number: 0,
                publication: None,
            });
        };
        let repo_info = crate::services::repo::get_repo_info(self.ctx.project_dir())
            .await
            .effect_err("agent")?;
        let mut live_pr_numbers = HashSet::new();
        for publication in publication_history_for_slice(&heads, slice_id) {
            if live_pr_numbers.contains(&publication.pr_number) {
                continue;
            }
            let pr = forgejo
                .get_pull_request(
                    &repo_info.owner,
                    &repo_info.repo,
                    PRNumber::new(publication.pr_number),
                )
                .await
                .effect_err("agent")?;
            if !pr.merged && pr.state.eq_ignore_ascii_case("open") {
                live_pr_numbers.insert(publication.pr_number);
            }
        }
        let resolution = resolve_live_pr_for_slice_with_abandonments(
            &heads,
            slice_id,
            &live_pr_numbers,
            &abandoned_attempts,
        );
        let (resolution_kind, pr_number) = match resolution {
            LivePrResolution::NeverPublished => (LivePrResolutionKind::NeverPublished, 0),
            LivePrResolution::AllAttemptsAbandoned => {
                (LivePrResolutionKind::AllAttemptsAbandoned, 0)
            }
            LivePrResolution::Live(pr_number) => (LivePrResolutionKind::Live, pr_number),
        };
        let publication = if pr_number > 0 {
            publication_history_for_slice(&heads, slice_id)
                .into_iter()
                .find(|head| head.pr_number == pr_number)
        } else {
            None
        };
        Ok(ResolveLivePrForSliceResponse {
            success: true,
            error: String::new(),
            slice_id: slice_id.to_string(),
            resolution: resolution_kind as i32,
            pr_number,
            publication: published_head_evidence(publication),
        })
    }

    async fn watcher_pr_state(
        &self,
        req: WatcherPrStateRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<WatcherPrStateResponse> {
        let pr_number = req.pr_number;
        if pr_number == 0 {
            return Err(EffectError::invalid_input("pr_number is required"));
        }

        let Some(forgejo) = self.ctx.forgejo_client() else {
            return Ok(watcher_pr_state_error(
                pr_number,
                "Forgejo is not configured; cannot query PR state",
            ));
        };
        let repo_info = match crate::services::repo::get_repo_info(self.ctx.project_dir()).await {
            Ok(repo_info) => repo_info,
            Err(error) => return Ok(watcher_pr_state_error(pr_number, error.to_string())),
        };

        let pr = match forgejo
            .get_pull_request(&repo_info.owner, &repo_info.repo, PRNumber::new(pr_number))
            .await
        {
            Ok(pr) => pr,
            Err(error) => return Ok(watcher_pr_state_error(pr_number, error.to_string())),
        };
        let head_sha = pr.head_sha.clone().unwrap_or_default();
        let project_dir = self.ctx.project_dir().to_string_lossy().into_owned();
        let (evidence, head_reachable, evidence_error) =
            match crate::services::merge_pr::observe_pr_evidence_for_pr(
                &project_dir,
                pr.base_ref.as_str(),
                &head_sha,
                pr_number,
            )
            .await
            {
                Ok(evidence) => (Some(evidence), true, String::new()),
                Err(error) if error.to_string().contains("pr_head_unreachable:") => {
                    (None, false, error.to_string())
                }
                Err(error) => return Ok(watcher_pr_state_error(pr_number, error.to_string())),
            };

        let reviews = match forgejo
            .list_pull_request_reviews(&repo_info.owner, &repo_info.repo, PRNumber::new(pr_number))
            .await
        {
            Ok(reviews) => reviews,
            Err(error) => return Ok(watcher_pr_state_error(pr_number, error.to_string())),
        };
        let (review_state, review_count) = review_state_from_forgejo_reviews(&reviews, &head_sha);
        let ci_status = if head_sha.is_empty() {
            CIStatus::Unknown
        } else {
            forgejo
                .commit_status_for_head(&repo_info.owner, &repo_info.repo, &head_sha)
                .await
                .unwrap_or(CIStatus::Unknown)
        };
        let (publication_ownership_verified, publication_ownership_error) =
            publication_ownership_status(
                self.ctx.as_ref(),
                pr_number,
                pr.head_ref.as_str(),
                pr.base_ref.as_str(),
                &head_sha,
            )
            .await;
        let publication = if publication_ownership_verified {
            read_published_heads(self.ctx.project_dir())
                .await
                .ok()
                .and_then(|heads| {
                    heads.into_iter().find(|head| {
                        head.matches_current(
                            pr_number,
                            pr.head_ref.as_str(),
                            pr.base_ref.as_str(),
                            &head_sha,
                        )
                    })
                })
        } else {
            None
        };
        let (
            review_id,
            review_verdict,
            review_head_sha,
            review_body,
            reviewer_agent_id,
            reviewer_identity_error,
        ) = match latest_exact_forgejo_review(&reviews, &head_sha) {
            None => (
                0,
                String::new(),
                String::new(),
                String::new(),
                String::new(),
                String::new(),
            ),
            Some(review) => {
                let review_id = review.id.unwrap_or_default();
                let review_verdict = normalized_review_verdict(&review.state)
                    .unwrap_or_default()
                    .to_string();
                let review_body = review.body.clone();
                let reviewer_login = match self.ctx.forgejo_reviewer_client() {
                    Some(client) => client.authenticated_user_login().await.ok().flatten(),
                    None => None,
                };
                let resolved = resolve_durable_review_author(
                    self.ctx.as_ref(),
                    pr_number,
                    &head_sha,
                    review.author_login.as_deref(),
                    reviewer_login.as_deref(),
                    publication
                        .as_ref()
                        .and_then(|value| value.author_agent.as_deref()),
                )
                .await;
                let owner = publication
                    .as_ref()
                    .and_then(|value| value.author_agent.as_deref());
                match resolved {
                    Some(agent_id) if owner != Some(agent_id.as_str()) => (
                        review_id,
                        review_verdict,
                        head_sha.clone(),
                        review_body.clone(),
                        agent_id,
                        String::new(),
                    ),
                    Some(_) => (
                        review_id,
                        review_verdict,
                        head_sha.clone(),
                        review_body.clone(),
                        String::new(),
                        "Forgejo review author is the PR owner".to_string(),
                    ),
                    None => (
                        review_id,
                        review_verdict,
                        head_sha.clone(),
                        review_body,
                        String::new(),
                        "Forgejo review author did not resolve to a registered agent".to_string(),
                    ),
                }
            }
        };
        Ok(WatcherPrStateResponse {
            success: true,
            error: String::new(),
            pr_number,
            found: true,
            review_state,
            ci_status: ci_status.as_str().to_string(),
            head_sha,
            head_branch: pr.head_ref.to_string(),
            base_branch: pr.base_ref.to_string(),
            base_sha: evidence
                .as_ref()
                .map(|value| value.base_sha.clone())
                .unwrap_or_default(),
            patch_digest: evidence
                .as_ref()
                .map(|value| value.patch_digest.clone())
                .unwrap_or_default(),
            merge_tree_sha: evidence
                .as_ref()
                .map(|value| value.merge_tree_sha.clone())
                .unwrap_or_default(),
            pr_state: pr.state,
            merged: pr.merged,
            review_count,
            head_reachable,
            evidence_error,
            publication_ownership_verified,
            publication_ownership_error,
            publication: published_head_evidence(publication.as_ref()),
            review_id,
            review_verdict,
            review_head_sha,
            reviewer_agent_id,
            reviewer_identity_error,
            review_body,
        })
    }

    async fn spawn_leaf_subtree(
        &self,
        req: SpawnLeafSubtreeRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnLeafSubtreeResponse> {
        self.ensure_tl_spawn_preflight(ctx).await?;
        if req.blocked_issue_id != 0
            || !req.expected_invocation_id.trim().is_empty()
            || !req.expected_branch.trim().is_empty()
            || !req.expected_worktree_fingerprint.trim().is_empty()
        {
            return self.resume_blocked_leaf(&req, ctx).await;
        }
        if req.resume_pr_number != 0 || !req.expected_head_sha.trim().is_empty() {
            return self.resume_existing_pr(&req, ctx).await;
        }
        self.reject_orphan_pr_spawn(&req).await?;
        if req.branch_name.trim().is_empty() {
            return Err(EffectError::invalid_input(
                "branch_name is required for an ordinary spawn",
            ));
        }
        let default_type = self.service.default_spawn_agent_type();
        let requested_agent_type = req.agent_type();
        let effective_agent_type =
            convert_agent_type_or_default(requested_agent_type, default_type)?;
        let model = self.service.effective_model_for(
            effective_agent_type,
            "dev",
            non_empty(req.model.clone()).as_deref(),
        );
        let effort = self.service.effective_effort_for("dev", None);
        self.enforce_harness_switch_policy(
            ctx,
            "spawn_leaf_subtree",
            default_type,
            requested_agent_type,
            effective_agent_type,
            model.clone(),
            effort.clone(),
        )?;
        info!(
            requested_agent_type = proto_agent_type_label(requested_agent_type),
            default_agent_type = default_type.suffix(),
            effective_agent_type = effective_agent_type.suffix(),
            branch_name = %req.branch_name,
            "Resolved spawn_leaf_subtree agent type"
        );
        let options = SpawnLeafOptions {
            task: req.task.clone(),
            branch_name: req.branch_name.clone(),
            role: non_empty(req.role.clone()).map(crate::domain::Role::new),
            agent_type: effective_agent_type,
            model: non_empty(req.model.clone()),
            claude_flags: claude_spawn_flags(
                req.permission_mode.clone(),
                req.allowed_tools.clone(),
                req.disallowed_tools.clone(),
            ),
            standalone_repo: req.standalone_repo,
            allowed_dirs: req.allowed_dirs,
            start_point: None,
            base_branch: None,
            expected_agent_name: None,
            invocation_pr_number: None,
            recovery_lineage: None,
        };

        let result = match self
            .service
            .spawn_leaf_subtree_with_intent(
                &options,
                &ctx.birth_branch,
                Some(req.intent_id.as_str()),
            )
            .await
            .effect_err_preserve("agent")
        {
            Ok(result) => result,
            Err(error) => {
                append_spawn_failed(
                    &self.ctx,
                    ctx.agent_name.as_ref(),
                    &req.branch_name,
                    &req.intent_id,
                    &error.to_string(),
                );
                return Err(error);
            }
        };
        let agent_info = leaf_subtree_result_to_proto(&req.branch_name, &result)?;
        let provenance = self.ctx.agent_resolver().get(&result.agent_name).await;
        let model = provenance
            .as_ref()
            .and_then(|record| record.model.clone())
            .or(model);
        let effort = provenance
            .as_ref()
            .and_then(|record| record.effort.clone())
            .or(effort);
        let topology = provenance
            .as_ref()
            .map(|record| format!("{:?}", record.topology).to_lowercase())
            .unwrap_or_else(|| "unknown".to_string());

        tracing::info!(
            otel.name = "agent.spawned",
            child_agent = %agent_info.id,
            agent_type = %AgentType::try_from(agent_info.agent_type).map(|t| format!("{:?}", t)).unwrap_or_else(|_| "unknown".to_string()),
            branch = %agent_info.branch_name,
            spawn_type = "leaf_subtree",
            model = ?model,
            effort = ?effort,
            topology = %topology,
            "[event] agent.spawned"
        );
        if let Some(log) = self.ctx.event_log() {
            let mut payload = serde_json::json!({
                "child_agent": agent_info.id, "agent_type": format!("{:?}", options.agent_type), "spawn_type": "leaf_subtree",
                "branch": agent_info.branch_name,
                "model": model,
                "effort": effort,
                "topology": topology,
            });
            if !req.intent_id.trim().is_empty() {
                payload["intent_id"] = serde_json::json!(req.intent_id);
            }
            let _ = log.append("agent.spawned", ctx.agent_name.as_ref(), &payload);
        }

        if options.agent_type == ServiceAgentType::Claude {
            // Claude Code leaves can participate in Claude Teams inboxes.
            let leaf_identity = crate::services::agent_control::AgentIdentity::new(
                crate::services::agent_control::slugify(&req.branch_name),
                options.agent_type,
            );
            let member_type_suffix = options.agent_type.suffix();
            self.register_claude_team_child(
                &leaf_identity.internal_name(),
                &format!("{}-leaf", member_type_suffix),
                &req.branch_name,
                ctx,
            )
            .await;
        } else {
            self.register_child_supervisor(agent_info.id.as_str(), ctx)
                .await;
        }

        capture_memory(
            ctx,
            self.ctx.as_ref(),
            spawned_child_capture(
                &agent_info.id,
                options.agent_type.suffix(),
                &agent_info.branch_name,
                "leaf_subtree",
                model.as_deref(),
                effort.as_deref(),
                &topology,
            ),
        );

        Ok(SpawnLeafSubtreeResponse {
            agent: Some(agent_info),
            invocation: None,
        })
    }

    async fn cleanup(
        &self,
        req: CleanupRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<CleanupResponse> {
        match self.service.cleanup_agent(&req.issue).await {
            Ok(_) => Ok(CleanupResponse {
                success: true,
                error: String::new(),
            }),
            Err(e) => Ok(CleanupResponse {
                success: false,
                error: e.to_string(),
            }),
        }
    }

    async fn dispose_orphan(
        &self,
        req: DisposeOrphanRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<DisposeOrphanResponse> {
        if req.sweep {
            if !req.verify_pr_state {
                return Err(EffectError::invalid_input("sweep requires verify_pr_state"));
            }
            return self.sweep_verified_orphans(req.dry_run).await;
        }

        let agent_slug = req.agent_slug.trim();
        if agent_slug.is_empty() {
            return Err(EffectError::invalid_input("agent_slug is required"));
        }
        if req.verify_pr_state {
            return self.dispose_verified_orphan(agent_slug, req.dry_run).await;
        }

        match orphan_agent_window_alive(self.ctx.project_dir(), agent_slug).await {
            Ok(true) => {
                return Err(EffectError::invalid_input(format!(
                    "Agent {agent_slug} window is still alive; refusing orphan cleanup"
                )));
            }
            Ok(false) => {}
            Err(error) => {
                return Err(EffectError::invalid_input(format!(
                    "Could not verify {agent_slug} is dead: {error}"
                )));
            }
        }

        let worktree_path = self
            .ctx
            .project_dir()
            .join(".exo/worktrees")
            .join(agent_slug);
        let agent_dir = self.ctx.project_dir().join(".exo/agents").join(agent_slug);
        let had_worktree = worktree_path.exists();
        let had_agent_dir = agent_dir.exists();

        dispose_agent_resources(
            self.ctx.project_dir(),
            self.ctx.git_worktree_service().clone(),
            agent_slug,
        )
        .await;

        let removed_worktree = had_worktree && !worktree_path.exists();
        let removed_agent_dir = had_agent_dir && !agent_dir.exists();
        Ok(DisposeOrphanResponse {
            removed_worktree,
            removed_agent_dir,
            message: format!(
                "Cleaned orphan {agent_slug}: worktree_removed={removed_worktree}, agent_dir_removed={removed_agent_dir}"
            ),
            pr_state: String::new(),
            pr_number: 0,
            verified: false,
            dry_run: false,
            cleaned_agents: Vec::new(),
            skipped_agents: Vec::new(),
            errors: Vec::new(),
        })
    }

    async fn cleanup_batch(
        &self,
        req: CleanupBatchRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<CleanupBatchResponse> {
        let subrepo = non_empty(req.subrepo);
        let result = self
            .service
            .cleanup_agents(&req.issues, subrepo.as_deref())
            .await;

        let failed_ids: Vec<String> = result.failed.iter().map(|(id, _)| id.clone()).collect();
        let errors: Vec<String> = result.failed.iter().map(|(_, err)| err.clone()).collect();

        Ok(CleanupBatchResponse {
            cleaned: result.cleaned,
            failed: failed_ids,
            errors,
        })
    }

    async fn cleanup_merged(
        &self,
        req: CleanupMergedRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<CleanupMergedResponse> {
        let subrepo = non_empty(req.subrepo);
        let result = self
            .service
            .cleanup_merged_agents(&req.issues, subrepo.as_deref())
            .await
            .effect_err("agent")?;

        let skipped: Vec<String> = result.failed.iter().map(|(id, _)| id.clone()).collect();
        let errors: Vec<String> = result.failed.iter().map(|(_, err)| err.clone()).collect();

        Ok(CleanupMergedResponse {
            cleaned: result.cleaned,
            skipped,
            errors,
        })
    }

    async fn list(
        &self,
        req: ListRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<ListResponse> {
        let infos = self.service.list_agents().await.effect_err("agent")?;
        let filter_type = non_empty(req.filter_type).map(|value| value.to_ascii_lowercase());
        let mut agents = Vec::new();

        for info in infos
            .iter()
            .filter(|info| agent_matches_filter(info, filter_type.as_deref()))
        {
            let (is_alive, routing_snapshot) = resolve_agent_liveness(info).await;
            if req.filter_alive_only && !is_alive {
                continue;
            }
            let agent_key = info.internal_name.as_str();
            let birth_branch = self
                .ctx
                .agent_resolver()
                .get(&info.internal_name)
                .await
                .map(|record| record.birth_branch.to_string())
                .unwrap_or_default();
            let has_unread = self
                .ctx
                .inbox_store()
                .has_unread(agent_key)
                .effect_err("agent")?;
            let last_check_inbox_at = self
                .ctx
                .inbox_store()
                .last_check_inbox_at(agent_key)
                .effect_err("agent")?
                .unwrap_or_default();
            let last_activity_at = info.last_activity_at.unwrap_or_default();

            agents.push(service_info_to_proto(
                info,
                AgentListMetadata {
                    birth_branch,
                    has_unread,
                    last_check_inbox_at,
                    last_activity_at,
                    is_alive,
                    last_known_routing: routing_snapshot.routing,
                    routing_retired: routing_snapshot.retired,
                    routing_exit_code: routing_snapshot.exit_code,
                },
            ));
        }

        Ok(ListResponse { agents })
    }

    async fn close_self(
        &self,
        _req: CloseSelfRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<CloseSelfResponse> {
        let agent_key = ctx.agent_name.to_string();
        let agents_dir = self.ctx.project_dir().join(".exo/agents");

        // FIXME: Routing is written under internal_name (slug-suffix, e.g. "beta-codex")
        // but MCP config passes bare slug as --name (e.g. "beta"). This suffix probing
        // is a band-aid — the real fix is making agent_name consistent between MCP config
        // and routing.json (either always include the suffix, or never).
        let candidates = std::iter::once(agent_key.clone()).chain(
            ["claude", "shoal", "opencode", "codex"]
                .iter()
                .map(|suffix| format!("{}-{}", agent_key, suffix)),
        );

        let mut routing = None;
        let mut resolved_internal_name = agent_key.clone();
        for candidate in candidates {
            let path = agents_dir.join(&candidate).join("routing.json");
            if let Ok(content) = std::fs::read_to_string(&path) {
                if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&content) {
                    info!(agent = %ctx.agent_name, path = %path.display(), "Found routing.json");
                    resolved_internal_name = candidate;
                    routing = Some(parsed);
                    break;
                }
            }
        }

        let mut closed = false;

        if let Some(ref r) = routing {
            let agent_dir = agents_dir.join(&resolved_internal_name);
            // Tombstone before killing the tmux target so future TL messages cannot route
            // through a stale routing.json if the pane/window disappears immediately.
            // The exact routing guard prevents an old close from killing a resumed
            // invocation that replaced this target.
            let Some(expected_routing) = serde_json::from_value::<RoutingInfo>(r.clone()).ok()
            else {
                warn!(agent = %ctx.agent_name, "Refusing to close agent with malformed routing metadata");
                return Ok(CloseSelfResponse {
                    success: false,
                    error: "malformed routing metadata".to_string(),
                });
            };
            if !tombstone_agent_dir_with_routing(&agent_dir, &expected_routing).await {
                warn!(agent = %ctx.agent_name, "Refusing to close stale agent routing target");
                return Ok(CloseSelfResponse {
                    success: false,
                    error: "agent routing changed before close".to_string(),
                });
            }

            // Try pane_id first (ephemeral workers)
            if let Some(pane_id) = r["pane_id"].as_str() {
                info!(agent = %ctx.agent_name, pane_id = %pane_id, "Closing worker pane");
                if let Err(e) = crate::services::tmux_events::close_worker_pane(pane_id).await {
                    warn!(agent = %ctx.agent_name, pane_id = %pane_id, error = %e, "Failed to close worker pane");
                } else {
                    closed = true;
                }
            }
            // Try window_id (worktree-based agents)
            else if let Some(window_id) = r["window_id"].as_str() {
                info!(agent = %ctx.agent_name, window_id = %window_id, "Closing agent window");
                let session = std::env::var("EXOMONAD_TMUX_SESSION")
                    .unwrap_or_else(|_| "exomonad".to_string());
                let ipc = crate::services::tmux_ipc::TmuxIpc::new(&session);
                match crate::services::tmux_ipc::WindowId::parse(window_id) {
                    Ok(wid) => {
                        if let Err(e) = ipc.kill_window(&wid).await {
                            warn!(agent = %ctx.agent_name, window_id = %window_id, error = %e, "Failed to close agent window");
                        } else {
                            closed = true;
                        }
                    }
                    Err(e) => {
                        warn!(agent = %ctx.agent_name, window_id = %window_id, error = %e, "Invalid window_id in routing.json");
                    }
                }
            } else {
                warn!(agent = %ctx.agent_name, "No pane_id or window_id in routing.json");
            }
        } else {
            warn!(agent = %ctx.agent_name, "Could not read routing.json (tried {agent_key} and suffixed variants)");
        }

        // Remove synthetic team member registration after closing.
        // AgentResolver is the canonical source for agent identity.
        if closed {
            {
                let team_reg = self.ctx.team_registry();
                let member_name = {
                    let resolver = self.ctx.agent_resolver();
                    let name =
                        crate::domain::AgentName::try_from_str(resolved_internal_name.as_str())
                            .expect("validated string input is non-empty");
                    if let Ok(records) = resolver.records_ref().try_read() {
                        records.get(&name).map(|r| r.agent_name.clone())
                    } else {
                        None
                    }
                };
                if let Some(member_name) = member_name {
                    let birth_branch_str = ctx.birth_branch.as_str();
                    let team_info = if let Some(info) = team_reg.get(&agent_key).await {
                        Some(info)
                    } else if let Some(info) = team_reg.get(birth_branch_str).await {
                        Some(info)
                    } else if let Some(parent) = ctx.birth_branch.parent() {
                        team_reg.get(parent.as_str()).await
                    } else {
                        None
                    };
                    if let Some(info) = team_info {
                        let team_name = TeamName::try_from_str(info.team_name.as_str())
                            .expect("validated string input is non-empty");
                        if let Err(e) = crate::services::synthetic_members::remove_synthetic_member(
                            &team_name,
                            &member_name,
                        ) {
                            warn!(team = %team_name, member = %member_name, error = %e, "Failed to remove synthetic member on close_self (non-fatal)");
                        }
                    }
                } else {
                    warn!(agent = %ctx.agent_name, "No resolver record for agent, skipping synthetic member cleanup");
                }
            }
        }

        info!(agent = %ctx.agent_name, closed, "Agent requested self-closure");

        Ok(CloseSelfResponse {
            success: closed,
            error: String::new(),
        })
    }

    async fn close_worker_pane(
        &self,
        req: CloseWorkerPaneRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<CloseWorkerPaneResponse> {
        if req.pane_id.is_empty() {
            return Ok(CloseWorkerPaneResponse {
                success: false,
                error: "pane_id is required".to_string(),
            });
        }

        match crate::services::tmux_events::close_worker_pane(&req.pane_id).await {
            Ok(()) => {
                tombstone_agent_by_pane(self.ctx.project_dir(), &req.pane_id).await;
                Ok(CloseWorkerPaneResponse {
                    success: true,
                    error: String::new(),
                })
            }
            Err(e) => {
                let cleaned = tombstone_agent_by_pane(self.ctx.project_dir(), &req.pane_id).await;
                Ok(CloseWorkerPaneResponse {
                    success: cleaned,
                    error: if cleaned {
                        String::new()
                    } else {
                        e.to_string()
                    },
                })
            }
        }
    }

    async fn close_reviewer_window(
        &self,
        req: CloseReviewerWindowRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<CloseReviewerWindowResponse> {
        if req.pr_number == 0 {
            return Err(EffectError::invalid_input("pr_number is required"));
        }

        match close_reviewer_windows_by_pr(&self.service, req.pr_number).await {
            Ok(closed_windows) => {
                let success = !closed_windows.is_empty();
                Ok(CloseReviewerWindowResponse {
                    success,
                    error: if success {
                        String::new()
                    } else {
                        format!(
                            "No reviewer tmux windows matched pattern review-pr-{}-",
                            req.pr_number
                        )
                    },
                    pr_number: req.pr_number,
                    closed_windows,
                })
            }
            Err(error) => Ok(CloseReviewerWindowResponse {
                success: false,
                error: error.to_string(),
                pr_number: req.pr_number,
                closed_windows: Vec::new(),
            }),
        }
    }

    async fn close_issue_and_cleanup(
        &self,
        req: CloseIssueAndCleanupRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<CloseIssueAndCleanupResponse> {
        if req.issue_id == 0 {
            return Ok(close_issue_cleanup_error("issue_id is required"));
        }
        if req.leaf_name.trim().is_empty() {
            return Ok(close_issue_cleanup_error("leaf_name is required"));
        }

        let open_prs = self
            .matching_open_forgejo_prs_for_cleanup(req.issue_id, &req.leaf_name)
            .await?;
        if !open_prs.is_empty() {
            return Ok(CloseIssueAndCleanupResponse {
                success: false,
                error: format!(
                    "Refusing cleanup: PR(s) {} for leaf '{}' are not merged",
                    format_pr_numbers(&open_prs),
                    req.leaf_name
                ),
                leaf_name: req.leaf_name,
                cleaned_pr_numbers: Vec::new(),
            });
        }

        if let Err(error) =
            close_chainlink_issue_for_cleanup(self.ctx.project_dir(), req.issue_id).await
        {
            return Ok(CloseIssueAndCleanupResponse {
                success: false,
                error,
                leaf_name: req.leaf_name,
                cleaned_pr_numbers: Vec::new(),
            });
        }

        if let Err(error) = self.service.cleanup_agent(&req.leaf_name).await {
            return Ok(CloseIssueAndCleanupResponse {
                success: false,
                error: error.to_string(),
                leaf_name: req.leaf_name,
                cleaned_pr_numbers: Vec::new(),
            });
        }

        Ok(CloseIssueAndCleanupResponse {
            success: true,
            error: String::new(),
            leaf_name: req.leaf_name,
            cleaned_pr_numbers: Vec::new(),
        })
    }
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

impl<
        C: HasTeamRegistry
            + HasAgentResolver
            + HasGitHubClient
            + HasProjectDir
            + HasGitWorktreeService
            + HasInboxStore
            + HasSessionMemory
            + HasSupervisorRegistry
            + HasClaudeSessionRegistry
            + HasEventLog
            + HasForgejoClient
            + HasWatcherRuntimeState
            + 'static,
    > AgentHandler<C>
{
    async fn dispose_verified_orphan(
        &self,
        agent_slug: &str,
        dry_run: bool,
    ) -> EffectResult<DisposeOrphanResponse> {
        let candidate = self
            .verify_orphan_cleanup(agent_slug)
            .await
            .map_err(EffectError::invalid_input)?;
        let mut response = DisposeOrphanResponse {
            removed_worktree: false,
            removed_agent_dir: false,
            message: format!(
                "Verified orphan {agent_slug}: PR #{} is {}; worktree is clean and tmux is dead",
                candidate.pr_number, candidate.pr_state
            ),
            pr_state: candidate.pr_state.clone(),
            pr_number: candidate.pr_number,
            verified: true,
            dry_run,
            cleaned_agents: Vec::new(),
            skipped_agents: Vec::new(),
            errors: Vec::new(),
        };
        if dry_run {
            info!(
                agent = %agent_slug,
                pr_number = candidate.pr_number,
                pr_state = %candidate.pr_state,
                "Verified cleanup_leaf dry run; resources were not disposed"
            );
            return Ok(response);
        }

        dispose_agent_resources(
            self.ctx.project_dir(),
            self.ctx.git_worktree_service().clone(),
            agent_slug,
        )
        .await;
        response.removed_worktree = !candidate.worktree_path.exists();
        response.removed_agent_dir = !candidate.agent_dir.exists();
        response.cleaned_agents.push(agent_slug.to_string());
        response.message = format!(
            "Disposed verified orphan {agent_slug}: PR #{} is {}; worktree_removed={}, agent_dir_removed={}",
            candidate.pr_number,
            candidate.pr_state,
            response.removed_worktree,
            response.removed_agent_dir
        );
        info!(
            agent = %agent_slug,
            pr_number = candidate.pr_number,
            pr_state = %candidate.pr_state,
            removed_worktree = response.removed_worktree,
            removed_agent_dir = response.removed_agent_dir,
            "Disposed verified cleanup_leaf target"
        );
        Ok(response)
    }

    async fn sweep_verified_orphans(&self, dry_run: bool) -> EffectResult<DisposeOrphanResponse> {
        let worktrees_dir = self.ctx.project_dir().join(".exo/worktrees");
        let mut entries = tokio::fs::read_dir(&worktrees_dir).await.map_err(|error| {
            EffectError::invalid_input(format!("Could not list orphan worktrees: {error}"))
        })?;
        let mut slugs = Vec::new();
        while let Some(entry) = entries.next_entry().await.map_err(|error| {
            EffectError::invalid_input(format!("Could not read orphan worktrees: {error}"))
        })? {
            if entry
                .file_type()
                .await
                .map_err(|error| {
                    EffectError::invalid_input(format!(
                        "Could not inspect orphan worktree: {error}"
                    ))
                })?
                .is_dir()
            {
                if let Some(slug) = entry.file_name().to_str() {
                    slugs.push(slug.to_string());
                }
            }
        }
        slugs.sort();

        let mut response = DisposeOrphanResponse {
            removed_worktree: false,
            removed_agent_dir: false,
            message: String::new(),
            pr_state: String::new(),
            pr_number: 0,
            verified: true,
            dry_run,
            cleaned_agents: Vec::new(),
            skipped_agents: Vec::new(),
            errors: Vec::new(),
        };
        for slug in slugs {
            match self.dispose_verified_orphan(&slug, dry_run).await {
                Ok(candidate) => {
                    response.removed_worktree |= candidate.removed_worktree;
                    response.removed_agent_dir |= candidate.removed_agent_dir;
                    response.cleaned_agents.extend(candidate.cleaned_agents);
                }
                Err(error) => {
                    warn!(agent = %slug, error = %error, "cleanup_leaf sweep refused orphan");
                    response.skipped_agents.push(slug.clone());
                    response.errors.push(format!("{slug}: {error}"));
                    response.verified = false;
                }
            }
        }
        response.message = format!(
            "cleanup_leaf sweep complete: cleaned={}, skipped={}, dry_run={dry_run}",
            response.cleaned_agents.len(),
            response.skipped_agents.len()
        );
        info!(
            cleaned = response.cleaned_agents.len(),
            skipped = response.skipped_agents.len(),
            dry_run,
            "Completed cleanup_leaf sweep"
        );
        Ok(response)
    }

    async fn verify_orphan_cleanup(&self, agent_slug: &str) -> Result<VerifiedOrphan, String> {
        info!(agent = %agent_slug, "Verifying cleanup_leaf target before disposal");
        match orphan_agent_window_alive(self.ctx.project_dir(), agent_slug).await {
            Ok(true) => return Err("tmux window or pane is still alive".to_string()),
            Ok(false) => {}
            Err(error) => return Err(format!("could not verify tmux is dead: {error}")),
        }

        let worktree_path = self
            .ctx
            .project_dir()
            .join(".exo/worktrees")
            .join(agent_slug);
        if !worktree_path.is_dir() {
            return Err(format!(
                "worktree does not exist: {}",
                worktree_path.display()
            ));
        }
        let dirty = dirty_worktree_entries(&worktree_path).await?;
        if !dirty.is_empty() {
            return Err(dirty_worktree_message(&dirty));
        }

        let output = Command::new("git")
            .arg("-C")
            .arg(&worktree_path)
            .args(["branch", "--show-current"])
            .output()
            .await
            .map_err(|error| format!("failed to read worktree branch: {error}"))?;
        if !output.status.success() {
            return Err(format!(
                "failed to read worktree branch: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        let branch = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if branch.is_empty() {
            return Err("worktree is detached or has no branch".to_string());
        }
        let branch = BranchName::try_from_str(&branch)
            .map_err(|error| format!("invalid worktree branch {branch}: {error}"))?;

        let forgejo = self
            .ctx
            .forgejo_client()
            .ok_or_else(|| "Forgejo is not configured; cannot verify PR state".to_string())?;
        let repo_info = crate::services::repo::get_repo_info(self.ctx.project_dir())
            .await
            .map_err(|error| format!("could not resolve repository for PR lookup: {error}"))?;
        let prs = forgejo
            .find_pull_requests_by_head(&repo_info.owner, &repo_info.repo, &branch)
            .await
            .map_err(|error| format!("could not query PR state: {error}"))?;
        if prs.len() != 1 {
            return Err(match prs.len() {
                0 => format!("no PR found for branch {branch}"),
                count => {
                    format!("found {count} PRs for branch {branch}; refusing ambiguous cleanup")
                }
            });
        }
        let pr = &prs[0];
        let pr_state = cleanup_pr_state(pr)?;
        Ok(VerifiedOrphan {
            worktree_path,
            agent_dir: self.ctx.project_dir().join(".exo/agents").join(agent_slug),
            pr_number: pr.number.as_u64(),
            pr_state,
        })
    }

    async fn resolve_forgejo_pr(&self, pr_number: u64) -> EffectResult<ForgejoPullRequest> {
        let Some(forgejo) = self.ctx.forgejo_client() else {
            return Err(EffectError::not_found(
                "Forgejo is not configured; cannot replace a closed PR",
            ));
        };
        let repo_info = crate::services::repo::get_repo_info(self.ctx.project_dir())
            .await
            .effect_err("agent")?;
        forgejo
            .get_pull_request(&repo_info.owner, &repo_info.repo, PRNumber::new(pr_number))
            .await
            .effect_err("agent")
    }

    async fn replacement_identity_conflict(
        &self,
        new_slug: &str,
        new_branch: &str,
        new_worktree_path: &Path,
    ) -> EffectResult<Option<String>> {
        let slug = Slug::try_from_str(new_slug).expect("validated replacement slug is non-empty");
        if self
            .service
            .agent_resolver()
            .lookup_by_slug(&slug)
            .await
            .is_some()
        {
            return Ok(Some(format!(
                "new leaf identity '{new_slug}' is already registered"
            )));
        }
        if new_worktree_path.exists()
            || self.service.worktree_base.join(new_slug).exists()
            || self.service.git_wt().branch_exists(
                &BranchName::try_from_str(new_branch)
                    .expect("validated replacement branch is non-empty"),
            )?
        {
            return Ok(Some(format!(
                "new leaf identity '{new_slug}' or branch '{new_branch}' already exists; choose a fresh slug"
            )));
        }
        Ok(None)
    }

    async fn matching_open_forgejo_prs_for_cleanup(
        &self,
        issue_id: u64,
        leaf_name: &str,
    ) -> EffectResult<Vec<u64>> {
        let Some(forgejo) = self.ctx.forgejo_client() else {
            return Ok(Vec::new());
        };
        let repo_info = crate::services::repo::get_repo_info(self.ctx.project_dir())
            .await
            .effect_err("agent")?;
        let prs = forgejo
            .list_open_pull_requests(&repo_info.owner, &repo_info.repo)
            .await
            .effect_err("agent")?;
        let mut numbers: Vec<u64> = prs
            .into_iter()
            .map(pr_entry_from_forgejo_pull_request)
            .filter(|pr| pr_matches_cleanup_target(pr, issue_id, leaf_name))
            .map(|pr| pr.number)
            .collect();
        numbers.sort_unstable();
        Ok(numbers)
    }

    async fn resolve_open_forgejo_pr_entry(&self, pr_number: u64) -> EffectResult<PrEntry> {
        let Some(forgejo) = self.ctx.forgejo_client() else {
            return Err(EffectError::not_found(
                "Forgejo is not configured; cannot spawn a reviewer for a PR",
            ));
        };
        let repo_info = crate::services::repo::get_repo_info(self.ctx.project_dir())
            .await
            .effect_err("agent")?;
        let pr = forgejo
            .get_pull_request(&repo_info.owner, &repo_info.repo, PRNumber::new(pr_number))
            .await
            .effect_err("agent")?;
        ensure_open_unmerged_pr(&pr, pr_number)?;
        Ok(pr_entry_from_forgejo_pull_request(pr))
    }
}

impl<
        C: HasTeamRegistry
            + HasAgentResolver
            + HasGitHubClient
            + HasProjectDir
            + HasGitWorktreeService
            + HasInboxStore
            + HasSessionMemory
            + HasSupervisorRegistry
            + HasClaudeSessionRegistry
            + HasEventLog
            + HasForgejoClient
            + HasWatcherRuntimeState
            + 'static,
    > AgentHandler<C>
{
    async fn resume_existing_pr(
        &self,
        req: &SpawnLeafSubtreeRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnLeafSubtreeResponse> {
        validate_resume_request(req)?;
        let forgejo = self.ctx.forgejo_client().ok_or_else(|| {
            EffectError::invalid_input("Forgejo is not configured; cannot resume a PR")
        })?;
        let repo_info = crate::services::repo::get_repo_info(self.ctx.project_dir())
            .await
            .effect_err("agent")?;
        let pr = forgejo
            .get_pull_request(
                &repo_info.owner,
                &repo_info.repo,
                PRNumber::new(req.resume_pr_number),
            )
            .await
            .effect_err("agent")?;
        ensure_open_unmerged_pr(&pr, req.resume_pr_number)?;

        let head_branch = pr.head_ref.as_str();
        let head_sha = pr
            .head_sha
            .as_deref()
            .filter(|sha| !sha.trim().is_empty())
            .ok_or_else(|| {
                EffectError::invalid_input(format!(
                    "PR #{} has no head SHA; refusing to resume it",
                    req.resume_pr_number
                ))
            })?;
        if head_sha != req.expected_head_sha.trim() {
            return Err(EffectError::invalid_input(format!(
                "PR #{} head SHA changed: expected {}, found {}; retry resume_pr to refresh state",
                req.resume_pr_number,
                req.expected_head_sha.trim(),
                head_sha
            )));
        }
        if head_branch.trim().is_empty() || pr.base_ref.as_str().trim().is_empty() {
            return Err(EffectError::invalid_input(format!(
                "PR #{} has an incomplete branch identity; refusing to resume it",
                req.resume_pr_number
            )));
        }

        let metadata = parse_pr_body_metadata(&pr.body);
        if let Some(metadata_branch) = metadata.birth_branch.as_deref() {
            if metadata_branch != head_branch {
                return Err(EffectError::invalid_input(format!(
                    "PR #{} Birth-Branch metadata '{}' does not match head branch '{}'",
                    req.resume_pr_number, metadata_branch, head_branch
                )));
            }
        }

        let owners = self
            .ctx
            .agent_resolver()
            .all()
            .await
            .into_iter()
            .filter(|record| {
                record.topology == Topology::WorktreePerAgent
                    && record.birth_branch.as_str() == head_branch
            })
            .collect::<Vec<_>>();
        let owner = match owners.as_slice() {
            [] => {
                return Err(EffectError::invalid_input(format!(
                    "PR #{} owner is unresolved for head branch '{}'; use replace_close_pr only with human approval",
                    req.resume_pr_number, head_branch
                )));
            }
            [owner] => owner,
            _ => {
                return Err(EffectError::invalid_input(format!(
                    "PR #{} has multiple owners for head branch '{}'; refusing to resume it",
                    req.resume_pr_number, head_branch
                )));
            }
        };
        if let Some(metadata_agent) = metadata.author_agent.as_deref() {
            let matches_owner = metadata_agent == owner.agent_name.as_str()
                || metadata_agent == owner.slug.as_str()
                || leaf_identity_matches(metadata_agent, owner.agent_name.as_str());
            if !matches_owner {
                return Err(EffectError::invalid_input(format!(
                    "PR #{} Authoring-Agent metadata '{}' does not match resolved owner '{}'",
                    req.resume_pr_number, metadata_agent, owner.agent_name
                )));
            }
        }
        let expected_birth_branch = BirthBranch::try_from_str(pr.base_ref.as_str())
            .map_err(|error| EffectError::invalid_input(error.to_string()))?
            .child(owner.agent_name.as_str());
        if expected_birth_branch.as_str() != head_branch {
            return Err(EffectError::invalid_input(format!(
                "PR #{} head branch '{}' is not the resolved owner's child of base branch '{}'",
                req.resume_pr_number, head_branch, pr.base_ref
            )));
        }

        let continuation_prefix = resume_pr_prefix(
            self.ctx.as_ref(),
            &self.service,
            &ctx.birth_branch,
            req.resume_pr_number,
            head_sha,
            owner,
        )
        .await;
        let options = SpawnLeafOptions {
            task: req.task.clone(),
            branch_name: owner.slug.to_string(),
            role: Some(crate::domain::Role::dev()),
            agent_type: owner.agent_type,
            claude_flags: ClaudeSpawnFlags::default(),
            standalone_repo: false,
            allowed_dirs: Vec::new(),
            start_point: Some(head_sha.to_string()),
            base_branch: Some(pr.base_ref.to_string()),
            expected_agent_name: Some(owner.agent_name.clone()),
            invocation_pr_number: Some(req.resume_pr_number),
            recovery_lineage: None,
            model: non_empty(req.model.clone()),
        };
        let options = with_resume_task(options, continuation_prefix.as_deref());
        let owner_dir = self
            .ctx
            .project_dir()
            .join(".exo/agents")
            .join(owner.agent_name.as_str());
        let previous_invocation = read_invocation(&owner_dir).await.effect_err("agent")?;
        let result = self
            .service
            .spawn_leaf_subtree(&options, &ctx.birth_branch)
            .await
            .effect_err_preserve("agent")?;
        let invocation = read_invocation(&owner_dir)
            .await
            .effect_err("agent")?
            .ok_or_else(|| {
                EffectError::invalid_input(format!(
                    "PR #{} resume failed: host did not persist a fresh invocation",
                    req.resume_pr_number
                ))
            })?;
        let fresh = invocation_is_fresh(previous_invocation.as_ref(), &invocation);
        if !fresh {
            return Err(EffectError::invalid_input(format!(
                "PR #{} owner is already running invocation {}; resume_pr did not create a fresh invocation",
                req.resume_pr_number, invocation.invocation_id
            )));
        }
        let target_alive = self.service.routing_liveness(&owner_dir).await == Some(true);
        if !invocation.is_live() || !target_alive {
            return Err(EffectError::invalid_input(format!(
                "PR #{} resume failed: {} target {} was not live at readiness confirmation",
                req.resume_pr_number,
                invocation_status_label(invocation.status),
                invocation_target_label(&invocation)
            )));
        }
        let mut agent_info = leaf_subtree_result_to_proto(owner.slug.as_str(), &result)?;
        agent_info.alive = true;
        agent_info.is_alive = true;
        let invocation_handoff = invocation_handoff_to_proto(
            &invocation,
            &result.branch_name,
            true,
            target_alive,
            "started",
        );

        if result.agent_type == ServiceAgentType::Claude {
            self.register_claude_team_child(
                &result.agent_name,
                &format!("{}-leaf", result.agent_type.suffix()),
                &result.branch_name,
                ctx,
            )
            .await;
        } else {
            self.register_child_supervisor(agent_info.id.as_str(), ctx)
                .await;
        }

        let model = invocation.model.clone().or_else(|| owner.model.clone());
        let effort = invocation.effort.clone().or_else(|| owner.effort.clone());
        let topology = format!("{:?}", owner.topology).to_lowercase();
        info!(
            pr_number = req.resume_pr_number,
            head_branch,
            head_sha,
            owner = %owner.agent_name,
            model = ?model,
            effort = ?effort,
            topology = %topology,
            "Resumed exact existing PR owner"
        );
        if let Some(log) = self.ctx.event_log() {
            let _ = log.append(
                "agent.resumed",
                ctx.agent_name.as_ref(),
                &serde_json::json!({
                    "child_agent": owner.agent_name,
                    "agent_type": owner.agent_type.suffix(),
                    "branch": head_branch,
                    "spawn_type": "resume_pr",
                    "pr_number": req.resume_pr_number,
                    "head_sha": head_sha,
                    "model": model,
                    "effort": effort,
                    "topology": topology,
                }),
            );
        }

        capture_memory(
            ctx,
            self.ctx.as_ref(),
            resume_fix_direction_capture(
                req.resume_pr_number,
                head_sha,
                owner.agent_name.as_str(),
                head_branch,
                &req.task,
                model.as_deref(),
                effort.as_deref(),
                &topology,
            ),
        );

        Ok(SpawnLeafSubtreeResponse {
            agent: Some(agent_info),
            invocation: Some(invocation_handoff),
        })
    }

    async fn resume_blocked_leaf(
        &self,
        req: &SpawnLeafSubtreeRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnLeafSubtreeResponse> {
        validate_blocked_resume_request(req)?;
        ensure_chainlink_issue_open(self.ctx.project_dir(), req.blocked_issue_id)
            .await
            .map_err(EffectError::invalid_input)?;

        let records = self.ctx.agent_resolver().all().await;
        let mut owners = Vec::new();
        for record in records {
            let agent_dir = self
                .ctx
                .project_dir()
                .join(".exo/agents")
                .join(record.agent_name.as_str());
            let active_issue = match fs::read_to_string(agent_dir.join("active_issue")).await {
                Ok(value) => value.trim().parse::<u64>().ok(),
                Err(_) => None,
            };
            if active_issue == Some(req.blocked_issue_id) {
                owners.push((record, agent_dir));
            }
        }
        let [(owner, owner_dir)] = owners.as_slice() else {
            return Err(EffectError::invalid_input(format!(
                "blocked issue #{} must resolve to exactly one dormant owner (found {})",
                req.blocked_issue_id,
                owners.len()
            )));
        };
        if owner.topology != Topology::WorktreePerAgent {
            return Err(EffectError::invalid_input(
                "blocked leaf resume requires an isolated worktree owner",
            ));
        }
        let parked_slice_id = owner.slice_id.as_deref().unwrap_or(owner.slug.as_str());
        let parked_event_seen = self
            .ctx
            .event_log()
            .ok_or_else(|| {
                EffectError::invalid_input(
                    "blocked leaf resume requires an authoritative parked event",
                )
            })?
            .ledger()
            .read_events()
            .effect_err("agent")?
            .into_iter()
            .rev()
            .any(|record| {
                let slice_matches = record
                    .event
                    .data
                    .get("slice_id")
                    .and_then(serde_json::Value::as_str)
                    == Some(parked_slice_id);
                let cause_matches = |key: &str| {
                    record
                        .event
                        .data
                        .get(key)
                        .and_then(serde_json::Value::as_str)
                        .is_some_and(|cause| {
                            matches!(
                                cause,
                                "base_ci_unstable"
                                    | "external_dependency"
                                    | "scope_boundary"
                                    | "human_decision_required"
                            )
                        })
                };
                slice_matches
                    && ((record.event.event_type == "tl.slice_parked"
                        && cause_matches("park_cause"))
                        || (record.event.event_type == "agent.task_blocked"
                            && record
                                .event
                                .data
                                .get("outcome")
                                .and_then(serde_json::Value::as_str)
                                == Some("blocked")
                            && cause_matches("cause")))
            });
        if !parked_event_seen {
            return Err(EffectError::invalid_input(format!(
                "blocked owner has no authoritative parked event for slice {parked_slice_id:?}"
            )));
        }
        let invocation = read_invocation(owner_dir)
            .await
            .effect_err("agent")?
            .ok_or_else(|| {
                EffectError::invalid_input(
                    "blocked owner has no invocation record; refusing resume",
                )
            })?;
        if invocation.invocation_id != req.expected_invocation_id {
            return Err(EffectError::invalid_input(format!(
                "blocked owner invocation changed: expected {}, found {}",
                req.expected_invocation_id, invocation.invocation_id
            )));
        }
        if invocation.is_live() {
            return Err(EffectError::invalid_input(
                "blocked owner invocation is still live; refusing duplicate resume",
            ));
        }
        if invocation.pr_number.is_some() {
            return Err(EffectError::invalid_input(
                "blocked owner already has PR context; use resume_pr",
            ));
        }
        if owner.birth_branch.as_str() != req.expected_branch {
            return Err(EffectError::invalid_input(format!(
                "blocked owner branch changed: expected {}, found {}",
                req.expected_branch, owner.birth_branch
            )));
        }

        let worktree = resolve_owner_worktree(self.ctx.project_dir(), owner)?;
        let actual_branch = git_current_branch(&worktree).await?;
        if actual_branch != req.expected_branch {
            return Err(EffectError::invalid_input(format!(
                "blocked worktree branch changed: expected {}, found {}",
                req.expected_branch, actual_branch
            )));
        }
        let fingerprint = worktree_fingerprint(&worktree).await?;
        if fingerprint != req.expected_worktree_fingerprint {
            return Err(EffectError::invalid_input(
                "blocked worktree fingerprint changed; refusing resume",
            ));
        }
        let agents = self.service.list_agents().await.effect_err("agent")?;
        if agents
            .iter()
            .any(|agent| agent.internal_name == owner.agent_name && agent.has_tab)
        {
            return Err(EffectError::invalid_input(
                "blocked owner still has a live tmux target; refusing duplicate resume",
            ));
        }

        let options = SpawnLeafOptions {
            task: format!(
                "{}\n\nResume the existing parked assignment for Chainlink issue #{}. Preserve the current branch and worktree; do not create a new owner or PR until the task is complete.",
                req.task.trim(),
                req.blocked_issue_id
            ),
            branch_name: owner.slug.to_string(),
            role: Some(crate::domain::Role::dev()),
            agent_type: owner.agent_type,
            claude_flags: ClaudeSpawnFlags::default(),
            standalone_repo: false,
            allowed_dirs: Vec::new(),
            start_point: None,
            base_branch: Some(owner.parent_branch.to_string()),
            expected_agent_name: Some(owner.agent_name.clone()),
            invocation_pr_number: None,
            recovery_lineage: Some(RecoveryInvocationLineage {
                prior_invocation_id: invocation.invocation_id.clone(),
                invocation_generation: invocation.generation.saturating_add(1),
                recovery_round: invocation.recovery_round.saturating_add(1),
                authorization_source: RecoveryAuthorization::HumanApproved,
            }),
            model: owner.model.clone(),
        };
        let result = self
            .service
            .spawn_leaf_subtree(&options, &ctx.birth_branch)
            .await
            .effect_err_preserve("agent")?;
        let fresh_invocation = read_invocation(owner_dir)
            .await
            .effect_err("agent")?
            .ok_or_else(|| {
                EffectError::invalid_input("blocked resume did not persist a fresh invocation")
            })?;
        if fresh_invocation.invocation_id == invocation.invocation_id || !fresh_invocation.is_live()
        {
            return Err(EffectError::invalid_input(
                "blocked resume did not start a fresh live invocation",
            ));
        }
        let target_alive = self.service.routing_liveness(owner_dir).await == Some(true);
        if !target_alive {
            return Err(EffectError::invalid_input(
                "blocked resume target was not live at readiness confirmation",
            ));
        }
        let mut agent_info = leaf_subtree_result_to_proto(owner.slug.as_str(), &result)?;
        agent_info.alive = true;
        agent_info.is_alive = true;
        let invocation_handoff = invocation_handoff_to_proto(
            &fresh_invocation,
            &result.branch_name,
            true,
            true,
            "started",
        );
        if let Some(log) = self.ctx.event_log() {
            let _ = log.append(
                "agent.resumed",
                ctx.agent_name.as_ref(),
                &serde_json::json!({
                    "child_agent": owner.agent_name,
                    "agent_type": owner.agent_type.suffix(),
                    "branch": owner.birth_branch,
                    "spawn_type": "resume_blocked",
                    "chainlink_issue_id": req.blocked_issue_id,
                    "previous_invocation_id": invocation.invocation_id,
                    "invocation_id": fresh_invocation.invocation_id,
                    "generation": fresh_invocation.generation,
                    "worktree_fingerprint": fingerprint,
                    "same_owner": true,
                }),
            );
        }
        Ok(SpawnLeafSubtreeResponse {
            agent: Some(agent_info),
            invocation: Some(invocation_handoff),
        })
    }
    async fn reject_orphan_pr_spawn(&self, req: &SpawnLeafSubtreeRequest) -> EffectResult<()> {
        let Some(pr_number) = referenced_pr_number(&req.task) else {
            return Ok(());
        };
        let Some(forgejo) = self.ctx.forgejo_client() else {
            return Ok(());
        };
        let repo_info = crate::services::repo::get_repo_info(self.ctx.project_dir())
            .await
            .effect_err("agent")?;
        let pr = forgejo
            .get_pull_request(&repo_info.owner, &repo_info.repo, PRNumber::new(pr_number))
            .await
            .effect_err("agent")?;
        if pr.merged || !pr.state.eq_ignore_ascii_case("open") {
            return Ok(());
        }

        let owners = self
            .ctx
            .agent_resolver()
            .all()
            .await
            .into_iter()
            .filter(|record| {
                record.topology == Topology::WorktreePerAgent
                    && record.birth_branch.as_str() == pr.head_ref.as_str()
            })
            .collect::<Vec<_>>();
        match owners.as_slice() {
            [] => Ok(()),
            [owner] => Err(EffectError::invalid_input(format!(
                "task references open PR #{pr_number} owned by {}; call resume_pr instead of spawning {}",
                owner.agent_name, req.branch_name
            ))),
            _ => Err(EffectError::invalid_input(format!(
                "task references open PR #{pr_number} with multiple persisted owners; call resume_pr instead of spawn_leaf"
            ))),
        }
    }
}

fn ensure_open_unmerged_pr(pr: &ForgejoPullRequest, pr_number: u64) -> EffectResult<()> {
    if pr.merged || !pr.state.eq_ignore_ascii_case("open") {
        return Err(EffectError::invalid_input(format!(
            "PR #{pr_number} is not open and unmerged; use replace_close_pr for a closed PR"
        )));
    }
    Ok(())
}

fn validate_resume_request(req: &SpawnLeafSubtreeRequest) -> EffectResult<()> {
    if req.resume_pr_number == 0 {
        return Err(EffectError::invalid_input(
            "resume_pr_number is required for a PR resume",
        ));
    }
    if req.expected_head_sha.trim().is_empty() {
        return Err(EffectError::invalid_input(
            "expected_head_sha is required for a PR resume",
        ));
    }
    if !req.branch_name.trim().is_empty() {
        return Err(EffectError::invalid_input(
            "resume_pr resolves the owning branch; branch_name must be omitted",
        ));
    }
    if !req.role.trim().is_empty()
        || req.agent_type() != AgentType::Unspecified
        || !req.permission_mode.trim().is_empty()
        || !req.allowed_tools.is_empty()
        || !req.disallowed_tools.is_empty()
        || !req.model.trim().is_empty()
        || req.standalone_repo
        || !req.allowed_dirs.is_empty()
    {
        return Err(EffectError::invalid_input(
            "resume_pr accepts only pr_number, expected_head_sha, and task; the host resolves all agent identity",
        ));
    }
    if req.task.trim().is_empty() {
        return Err(EffectError::invalid_input(
            "task is required for a PR resume",
        ));
    }
    Ok(())
}

fn validate_blocked_resume_request(req: &SpawnLeafSubtreeRequest) -> EffectResult<()> {
    if req.blocked_issue_id == 0 {
        return Err(EffectError::invalid_input(
            "blocked_issue_id is required for a parked-leaf resume",
        ));
    }
    for (name, value) in [
        ("expected_invocation_id", req.expected_invocation_id.trim()),
        ("expected_branch", req.expected_branch.trim()),
        (
            "expected_worktree_fingerprint",
            req.expected_worktree_fingerprint.trim(),
        ),
        ("task", req.task.trim()),
    ] {
        if value.is_empty() {
            return Err(EffectError::invalid_input(format!("{name} is required")));
        }
    }
    if !req.human_approved {
        return Err(EffectError::invalid_input(
            "human_approved must be true before resuming a parked leaf",
        ));
    }
    if req.resume_pr_number != 0 || !req.expected_head_sha.trim().is_empty() {
        return Err(EffectError::invalid_input(
            "a parked-leaf resume cannot include PR resume fields",
        ));
    }
    if !req.intent_id.trim().is_empty() {
        return Err(EffectError::invalid_input(
            "parked-leaf resume intent is host-owned and must be omitted",
        ));
    }
    if !req.branch_name.trim().is_empty()
        || !req.role.trim().is_empty()
        || req.agent_type() != AgentType::Unspecified
        || !req.permission_mode.trim().is_empty()
        || !req.allowed_tools.is_empty()
        || !req.disallowed_tools.is_empty()
        || !req.model.trim().is_empty()
        || req.standalone_repo
        || !req.allowed_dirs.is_empty()
    {
        return Err(EffectError::invalid_input(
            "parked-leaf resume accepts only issue, identity proofs, task, and approval",
        ));
    }
    Ok(())
}

fn resolve_owner_worktree(
    project_dir: &Path,
    owner: &AgentIdentityRecord,
) -> EffectResult<PathBuf> {
    let path = if owner.working_dir.is_absolute() {
        owner.working_dir.clone()
    } else {
        project_dir.join(&owner.working_dir)
    };
    if !path.starts_with(project_dir) {
        return Err(EffectError::invalid_input(
            "blocked owner worktree is outside the project",
        ));
    }
    if !path.is_dir() {
        return Err(EffectError::invalid_input(format!(
            "blocked owner worktree does not exist: {}",
            path.display()
        )));
    }
    Ok(path)
}

async fn git_current_branch(worktree: &Path) -> EffectResult<String> {
    let output = Command::new("git")
        .args(["branch", "--show-current"])
        .current_dir(worktree)
        .output()
        .await
        .map_err(|error| {
            EffectError::invalid_input(format!("failed to inspect worktree branch: {error}"))
        })?;
    if !output.status.success() {
        return Err(EffectError::invalid_input(
            "git could not determine the blocked worktree branch",
        ));
    }
    let branch = String::from_utf8_lossy(&output.stdout).trim().to_string();
    if branch.is_empty() {
        return Err(EffectError::invalid_input(
            "blocked worktree is detached; refusing resume",
        ));
    }
    Ok(branch)
}

async fn worktree_fingerprint(worktree: &Path) -> EffectResult<String> {
    let output = Command::new("git")
        .args(["status", "--porcelain=v1", "-z"])
        .current_dir(worktree)
        .output()
        .await
        .map_err(|error| {
            EffectError::invalid_input(format!("failed to inspect worktree state: {error}"))
        })?;
    if !output.status.success() {
        return Err(EffectError::invalid_input(
            "git could not determine the blocked worktree fingerprint",
        ));
    }
    Ok(format!("sha256:{:x}", Sha256::digest(&output.stdout)))
}

fn with_resume_task(options: SpawnLeafOptions, prefix: Option<&str>) -> SpawnLeafOptions {
    SpawnLeafOptions {
        task: prefix_task(prefix, &options.task),
        ..options
    }
}

fn ensure_replaceable_unmerged_pr(
    pr: &ForgejoPullRequest,
    pr_number: u64,
) -> Result<(), EffectError> {
    if pr.merged {
        return Err(EffectError::invalid_input(format!(
            "PR #{pr_number} is merged; replacement requires an unmerged PR"
        )));
    }
    if !pr.state.eq_ignore_ascii_case("closed") && !pr.state.eq_ignore_ascii_case("open") {
        return Err(EffectError::invalid_input(format!(
            "PR #{pr_number} has unsupported state '{}'; replacement requires an open or closed PR",
            pr.state
        )));
    }
    Ok(())
}

fn leaf_identity_matches(metadata_name: &str, requested_name: &str) -> bool {
    if metadata_name == requested_name {
        return true;
    }
    let has_runtime_suffix = ["-claude", "-shoal", "-opencode", "-codex"]
        .iter()
        .any(|suffix| metadata_name.ends_with(suffix));
    !has_runtime_suffix
        && AgentIdentity::from_internal_name(metadata_name).slug()
            == AgentIdentity::from_internal_name(requested_name).slug()
}

fn referenced_pr_number(task: &str) -> Option<u64> {
    let task = task.to_ascii_lowercase();
    ["pull request #", "pr #", "pr#"].iter().find_map(|marker| {
        let start = task.find(marker)? + marker.len();
        let rest = &task[start..];
        let digits = rest
            .bytes()
            .take_while(u8::is_ascii_digit)
            .collect::<Vec<_>>();
        if digits.is_empty()
            || rest
                .as_bytes()
                .get(digits.len())
                .is_some_and(|byte| byte.is_ascii_alphanumeric())
        {
            return None;
        }
        std::str::from_utf8(&digits).ok()?.parse::<u64>().ok()
    })
}

async fn ensure_chainlink_issue_open(project_dir: &Path, issue_id: u64) -> Result<(), String> {
    let output = Command::new("chainlink")
        .args(["show", &issue_id.to_string()])
        .current_dir(project_dir)
        .output()
        .await
        .map_err(|error| format!("failed to inspect Chainlink issue #{issue_id}: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "Chainlink issue #{issue_id} could not be inspected: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    if stdout
        .lines()
        .any(|line| line.trim().eq_ignore_ascii_case("Status: closed"))
    {
        return Err(format!(
            "Chainlink issue #{issue_id} is closed; replacement must continue the existing open issue"
        ));
    }
    if !stdout
        .lines()
        .any(|line| line.trim().to_ascii_lowercase().starts_with("status:"))
    {
        return Err(format!(
            "Chainlink issue #{issue_id} status was not returned; refusing replacement"
        ));
    }
    Ok(())
}

async fn ensure_git_revision_reachable(
    project_dir: &Path,
    source_branch: &str,
    source_sha: &str,
) -> Result<(), String> {
    let revision = format!("{source_sha}^{{commit}}");
    let reachable = Command::new("git")
        .args(["cat-file", "-e", &revision])
        .current_dir(project_dir)
        .output()
        .await
        .map_err(|error| format!("failed to inspect source SHA {source_sha}: {error}"))?;
    if reachable.status.success() {
        return Ok(());
    }

    let fetched = Command::new("git")
        .args(["fetch", "--all", "--prune"])
        .current_dir(project_dir)
        .output()
        .await
        .map_err(|error| format!("failed to fetch source branch '{source_branch}': {error}"))?;
    if !fetched.status.success() {
        return Err(format!(
            "source SHA {source_sha} for branch '{source_branch}' is unavailable and fetch failed: {}",
            String::from_utf8_lossy(&fetched.stderr).trim()
        ));
    }

    let reachable_after_fetch = Command::new("git")
        .args(["cat-file", "-e", &revision])
        .current_dir(project_dir)
        .output()
        .await
        .map_err(|error| format!("failed to verify fetched source SHA {source_sha}: {error}"))?;
    if reachable_after_fetch.status.success() {
        Ok(())
    } else {
        Err(format!(
            "source SHA {source_sha} for branch '{source_branch}' is not reachable locally or from configured remotes"
        ))
    }
}

fn old_leaf_worktree_exists(worktree_base: &Path, identity: &AgentIdentity) -> bool {
    worktree_base.join(identity.slug()).exists()
        || worktree_base
            .join(identity.internal_name().as_str())
            .exists()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ReplacementRecord {
    chainlink_issue_id: u64,
    old_pr_number: u64,
    old_pr_state: String,
    old_pr_merged: bool,
    old_head_branch: String,
    source_head_sha: String,
    original_base_branch: String,
    old_leaf_name: String,
    new_leaf_name: String,
    new_branch: String,
    worktree_path: String,
    retired_resources: Vec<String>,
    spawn_status: String,
    error: String,
}

impl ReplacementRecord {
    #[allow(clippy::too_many_arguments)]
    fn new(
        chainlink_issue_id: u64,
        old_pr_number: u64,
        old_pr_state: &str,
        old_pr_merged: bool,
        old_head_branch: &str,
        source_head_sha: &str,
        original_base_branch: &str,
        old_leaf_name: &str,
        new_leaf_name: &str,
        new_branch: &str,
        worktree_path: &Path,
    ) -> Self {
        Self {
            chainlink_issue_id,
            old_pr_number,
            old_pr_state: old_pr_state.to_string(),
            old_pr_merged,
            old_head_branch: old_head_branch.to_string(),
            source_head_sha: source_head_sha.to_string(),
            original_base_branch: original_base_branch.to_string(),
            old_leaf_name: old_leaf_name.to_string(),
            new_leaf_name: new_leaf_name.to_string(),
            new_branch: new_branch.to_string(),
            worktree_path: worktree_path.to_string_lossy().to_string(),
            retired_resources: Vec::new(),
            spawn_status: "pending".to_string(),
            error: String::new(),
        }
    }

    fn matches_request(
        &self,
        issue_id: u64,
        old_pr_number: u64,
        old_leaf_name: &str,
        new_leaf_name: &str,
        source_head_sha: &str,
        original_base_branch: &str,
    ) -> bool {
        self.chainlink_issue_id == issue_id
            && self.old_pr_number == old_pr_number
            && self.old_leaf_name == old_leaf_name
            && self.new_leaf_name == new_leaf_name
            && self.source_head_sha == source_head_sha
            && self.original_base_branch == original_base_branch
    }

    fn to_response(&self, already_exists: bool) -> ReplaceClosedPrResponse {
        let success = self.spawn_status == "spawned";
        let next_action = if success {
            if self.old_pr_state.eq_ignore_ascii_case("open") {
                format!(
                    "Wait for the fresh leaf to file a new PR, then explicitly reconcile or close superseded PR #{}",
                    self.old_pr_number
                )
            } else {
                "Wait for the fresh leaf to file a new PR against the original base branch"
                    .to_string()
            }
        } else {
            "Inspect the reported state and retry replace_close_pr; the old PR head and replacement record are preserved"
                .to_string()
        };
        ReplaceClosedPrResponse {
            success,
            error: self.error.clone(),
            chainlink_issue_id: self.chainlink_issue_id,
            old_pr_number: self.old_pr_number,
            old_pr_state: self.old_pr_state.clone(),
            old_pr_merged: self.old_pr_merged,
            old_head_branch: self.old_head_branch.clone(),
            source_head_sha: self.source_head_sha.clone(),
            original_base_branch: self.original_base_branch.clone(),
            old_leaf_name: self.old_leaf_name.clone(),
            retired_resources: self.retired_resources.clone(),
            new_leaf_name: self.new_leaf_name.clone(),
            new_branch: self.new_branch.clone(),
            worktree_path: self.worktree_path.clone(),
            spawn_status: self.spawn_status.clone(),
            next_action,
            replacement_already_exists: already_exists,
        }
    }
}

fn replace_closed_pr_error(message: &str) -> ReplaceClosedPrResponse {
    ReplaceClosedPrResponse {
        success: false,
        error: message.to_string(),
        ..Default::default()
    }
}

fn replacement_record_path(project_dir: &Path, old_pr_number: u64) -> PathBuf {
    project_dir
        .join(".exo/replacements")
        .join(format!("pr-{old_pr_number}.json"))
}

async fn read_replacement_record(path: &Path) -> EffectResult<Option<ReplacementRecord>> {
    match tokio::fs::read_to_string(path).await {
        Ok(contents) => serde_json::from_str(&contents)
            .map(Some)
            .effect_err("agent"),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(None),
        Err(error) => Err(EffectError::custom(
            "replacement_record_error",
            format!("failed to read {}: {error}", path.display()),
        )),
    }
}

async fn write_replacement_record(path: &Path, record: &ReplacementRecord) -> EffectResult<()> {
    tokio::fs::create_dir_all(
        path.parent()
            .expect("replacement record path always has a parent directory"),
    )
    .await
    .effect_err("agent")?;
    let contents = serde_json::to_vec_pretty(record).effect_err("agent")?;
    tokio::fs::write(path, contents).await.effect_err("agent")
}

fn pr_entry_from_forgejo_pull_request(pr: ForgejoPullRequest) -> PrEntry {
    let metadata = parse_pr_body_metadata(&pr.body);
    let birth_branch = metadata
        .birth_branch
        .as_deref()
        .unwrap_or(pr.head_ref.as_ref());
    let author_agent = metadata
        .author_agent
        .or_else(|| author_agent_from_branch(birth_branch))
        .unwrap_or_else(|| pr.head_ref.to_string());
    let author_role = metadata.author_role.unwrap_or_else(|| "dev".to_string());

    PrEntry {
        number: pr.number.as_u64(),
        head_branch: pr.head_ref.to_string(),
        base_branch: pr.base_ref.to_string(),
        title: pr.title,
        body: pr.body,
        author_agent,
        author_role,
        created_at: Utc::now(),
        state: PrState::Open,
        last_review_at: None,
        last_head_sha: pr.head_sha,
        approved_at_sha: None,
        reviewer_agent: metadata.reviewer_agent,
        reviewer_birth_branch: metadata.reviewer_birth_branch,
        rounds: 0,
        stuck: false,
        needs_human_review: false,
        merge_blocked_on_ci: false,
        chainlink_issue_id: metadata.chainlink_issue_id,
    }
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

async fn live_reviewer_for_pr<C>(service: &AgentControlService<C>, pr_number: u64) -> Option<String>
where
    C: HasTeamRegistry
        + HasAgentResolver
        + HasGitHubClient
        + HasProjectDir
        + HasGitWorktreeService
        + 'static,
{
    let tmux = match service.tmux() {
        Ok(tmux) => tmux,
        Err(error) => {
            warn!(%error, "failed to create tmux client while checking reviewer liveness");
            return None;
        }
    };
    let windows = match tmux.list_windows().await {
        Ok(windows) => windows,
        Err(error) => {
            warn!(%error, "failed to list tmux windows while checking reviewer liveness");
            return None;
        }
    };

    windows
        .into_iter()
        .find(|window| reviewer_window_matches_pr(&window.window_name, pr_number))
        .map(|window| window.window_name)
}

async fn clear_reviewer_review_artifacts(project_dir: &Path, pr_number: u64) -> anyhow::Result<()> {
    remove_legacy_review_file(project_dir, pr_number).await?;
    clear_watcher_pr_state(project_dir, pr_number).await
}

fn cleanup_reviewer_leaf_response(
    pr_number: u64,
    cleaned_reviewers: Vec<String>,
    artifacts_result: anyhow::Result<()>,
) -> CleanupReviewerLeafResponse {
    match artifacts_result {
        Err(error) => CleanupReviewerLeafResponse {
            success: false,
            error: error.to_string(),
            pr_number,
            cleaned_reviewers,
        },
        Ok(()) if cleaned_reviewers.is_empty() => CleanupReviewerLeafResponse {
            success: false,
            error: format!("No reviewer tmux windows matched pattern review-pr-{pr_number}-"),
            pr_number,
            cleaned_reviewers,
        },
        Ok(()) => CleanupReviewerLeafResponse {
            success: true,
            error: String::new(),
            pr_number,
            cleaned_reviewers,
        },
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct RestartReviewArtifactReset {
    watcher_state_found: bool,
    legacy_review_file_removed: bool,
}

async fn reset_reviewer_restart_artifacts(
    project_dir: &Path,
    pr_number: u64,
) -> anyhow::Result<RestartReviewArtifactReset> {
    let legacy_review_file_removed = remove_legacy_review_file(project_dir, pr_number).await?;
    let watcher_state_found = reset_watcher_pr_state_file(project_dir, pr_number).await?;
    Ok(RestartReviewArtifactReset {
        watcher_state_found,
        legacy_review_file_removed,
    })
}

async fn remove_legacy_review_file(project_dir: &Path, pr_number: u64) -> anyhow::Result<bool> {
    let review_path = project_dir
        .join(".exo/reviews")
        .join(format!("pr_{pr_number}.json"));
    match tokio::fs::remove_file(&review_path).await {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
    }
}

async fn clear_watcher_pr_state(project_dir: &Path, pr_number: u64) -> anyhow::Result<()> {
    let state_path = project_dir.join(".exo/watcher-state.json");
    let state = match tokio::fs::read_to_string(&state_path).await {
        Ok(state) => state,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
    };
    let mut value: serde_json::Value = serde_json::from_str(&state)?;
    if let Some(prs) = value
        .get_mut("prs")
        .and_then(serde_json::Value::as_object_mut)
    {
        prs.remove(&pr_number.to_string());
    }
    tokio::fs::write(&state_path, serde_json::to_vec_pretty(&value)?).await?;
    Ok(())
}

async fn reset_watcher_pr_state_file(project_dir: &Path, pr_number: u64) -> anyhow::Result<bool> {
    let state_path = project_dir.join(".exo/watcher-state.json");
    let state = match tokio::fs::read_to_string(&state_path).await {
        Ok(state) => state,
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error.into()),
    };
    let mut value: serde_json::Value = serde_json::from_str(&state)?;
    let found = value
        .get_mut("prs")
        .and_then(serde_json::Value::as_object_mut)
        .and_then(|prs| prs.get_mut(&pr_number.to_string()))
        .and_then(serde_json::Value::as_object_mut)
        .map(|entry| {
            entry.insert("rounds".to_string(), serde_json::json!(0));
            entry.insert("stuck".to_string(), serde_json::json!(false));
            entry.insert("needs_human_review".to_string(), serde_json::json!(false));
            entry.insert("reviewer_attempt".to_string(), serde_json::Value::Null);
            entry.remove("last_review_fingerprint");
        })
        .is_some();

    if found {
        tokio::fs::write(&state_path, serde_json::to_vec_pretty(&value)?).await?;
    }
    Ok(found)
}

async fn cleanup_force_reviewer_resources<C>(
    service: &AgentControlService<C>,
    pr_number: u64,
) -> Vec<String>
where
    C: HasTeamRegistry
        + HasAgentResolver
        + HasGitHubClient
        + HasProjectDir
        + HasGitWorktreeService
        + 'static,
{
    match close_reviewer_windows_by_pr(service, pr_number).await {
        Ok(killed) => {
            if !killed.is_empty() {
                tombstone_reviewer_agent_dirs(service.project_dir(), pr_number).await;
            }
            killed
        }
        Err(error) => {
            warn!(%error, "failed to clean reviewer resources");
            Vec::new()
        }
    }
}

async fn tombstone_reviewer_agent_dirs(project_dir: &Path, pr_number: u64) {
    let agents_dir = project_dir.join(".exo/agents");
    let Ok(mut entries) = tokio::fs::read_dir(&agents_dir).await else {
        return;
    };
    let prefix = format!("review-pr-{pr_number}-");

    while let Ok(Some(entry)) = entries.next_entry().await {
        let Ok(file_type) = entry.file_type().await else {
            continue;
        };
        if !file_type.is_dir() {
            continue;
        }
        if entry.file_name().to_string_lossy().starts_with(&prefix) {
            tombstone_agent_dir(&entry.path()).await;
        }
    }
}

async fn close_reviewer_windows_by_pr<C>(
    service: &AgentControlService<C>,
    pr_number: u64,
) -> anyhow::Result<Vec<String>>
where
    C: HasTeamRegistry
        + HasAgentResolver
        + HasGitHubClient
        + HasProjectDir
        + HasGitWorktreeService
        + 'static,
{
    let tmux = service.tmux()?;
    let windows = tmux.list_windows().await?;

    let mut killed = Vec::new();
    for window in windows {
        if !reviewer_window_matches_pr(&window.window_name, pr_number) {
            continue;
        }
        if let Err(error) = tmux.kill_window(&window.window_id).await {
            warn!(window = %window.window_name, %error, "failed to kill reviewer tmux window");
        } else {
            info!(window = %window.window_name, "killed reviewer tmux window");
            killed.push(window.window_name);
        }
    }
    Ok(killed)
}

fn reviewer_window_matches_pr(window_name: &str, pr_number: u64) -> bool {
    window_name.contains(&format!("review-pr-{pr_number}-"))
}

async fn tombstone_agent_dir(agent_dir: &Path) {
    let Ok(routing) = RoutingInfo::read_from_dir(agent_dir).await else {
        warn!(path = %agent_dir.display(), "refusing to tombstone agent without valid routing");
        return;
    };
    let _ = tombstone_agent_dir_with_routing(agent_dir, &routing).await;
}

async fn tombstone_agent_dir_with_routing(agent_dir: &Path, routing: &RoutingInfo) -> bool {
    match finish_invocation_and_tombstone(agent_dir, routing, InvocationStatus::Killed, None).await
    {
        Ok(InvocationFinishResult::IgnoredStale) => false,
        Ok(InvocationFinishResult::Finished(_)) | Ok(InvocationFinishResult::Missing) => {
            info!(path = %agent_dir.display(), "retired agent routing after exit");
            true
        }
        Err(error) => {
            warn!(
                path = %agent_dir.display(),
                %error,
                "failed to finish invocation; preserving agent routing"
            );
            false
        }
    }
}

async fn tombstone_agent_by_pane(project_dir: &Path, pane_id: &str) -> bool {
    let agents_dir = project_dir.join(".exo/agents");
    let Ok(mut entries) = tokio::fs::read_dir(&agents_dir).await else {
        return false;
    };

    while let Ok(Some(entry)) = entries.next_entry().await {
        let Ok(file_type) = entry.file_type().await else {
            continue;
        };
        if !file_type.is_dir() {
            continue;
        }
        let agent_dir = entry.path();
        let Ok(routing) = RoutingInfo::read_from_dir(&agent_dir).await else {
            continue;
        };
        if routing
            .pane_id
            .as_ref()
            .is_some_and(|candidate| candidate.as_str() == pane_id)
        {
            return tombstone_agent_dir_with_routing(&agent_dir, &routing).await;
        }
    }
    false
}

async fn orphan_agent_window_alive(project_dir: &Path, agent_slug: &str) -> Result<bool, String> {
    let agent_dir = project_dir.join(".exo/agents").join(agent_slug);
    let Ok(routing) = RoutingInfo::read_from_dir(&agent_dir).await else {
        return Ok(false);
    };
    if routing.window_id.is_none() && routing.pane_id.is_none() {
        return Ok(false);
    }
    let session = std::env::var("EXOMONAD_TMUX_SESSION")
        .map_err(|_| "EXOMONAD_TMUX_SESSION is not set".to_string())?;
    if session.trim().is_empty() {
        return Err("EXOMONAD_TMUX_SESSION is empty".to_string());
    }
    let tmux = crate::services::tmux_ipc::TmuxIpc::new(&session);
    crate::services::tmux_ipc::routing_target_alive(&routing, &tmux)
        .await
        .map_err(|error| error.to_string())
}

fn close_issue_cleanup_error(message: &str) -> CloseIssueAndCleanupResponse {
    CloseIssueAndCleanupResponse {
        success: false,
        error: message.to_string(),
        leaf_name: String::new(),
        cleaned_pr_numbers: Vec::new(),
    }
}

async fn close_chainlink_issue_for_cleanup(
    project_dir: &Path,
    issue_id: u64,
) -> Result<(), String> {
    let output = tokio::process::Command::new("chainlink")
        .args(["close", &issue_id.to_string()])
        .current_dir(project_dir)
        .output()
        .await
        .map_err(|error| format!("failed to run chainlink close {issue_id}: {error}"))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "chainlink close {issue_id} failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    }
}

#[cfg(test)]
fn cleanup_prs_for_leaf<'a>(
    registry: &'a PrRegistry,
    issue_id: u64,
    leaf_name: &str,
) -> Vec<(u64, &'a PrEntry)> {
    let mut prs: Vec<(u64, &PrEntry)> = registry
        .prs
        .iter()
        .filter(|(_, pr)| pr_matches_cleanup_target(pr, issue_id, leaf_name))
        .map(|(number, pr)| (*number, pr))
        .collect();
    prs.sort_by_key(|(number, _)| *number);
    prs
}

fn pr_matches_cleanup_target(pr: &PrEntry, issue_id: u64, leaf_name: &str) -> bool {
    pr.chainlink_issue_id == Some(issue_id)
        || pr.author_agent == leaf_name
        || pr
            .head_branch
            .rsplit_once('.')
            .map(|(_, agent)| agent == leaf_name)
            .unwrap_or(false)
}

fn format_pr_numbers(numbers: &[u64]) -> String {
    numbers
        .iter()
        .map(u64::to_string)
        .collect::<Vec<_>>()
        .join(", ")
}

fn spawn_result_to_proto(
    issue: &str,
    result: &crate::services::agent_control::SpawnResult,
) -> exomonad_proto::effects::agent::AgentInfo {
    use crate::services::agent_control::Topology;

    exomonad_proto::effects::agent::AgentInfo {
        id: format!("{}-{}", issue, result.agent_type.suffix()),
        issue: issue.to_string(),
        worktree_path: result.worktree_path.display().to_string(),
        branch_name: String::new(),
        agent_type: service_agent_type_to_proto(result.agent_type),
        role: 0,
        alive: true,
        mux_window: result.agent_name.to_string(),
        error: String::new(),
        pr_number: 0,
        pr_url: String::new(),
        topology: Topology::WorktreePerAgent.to_proto(),
        pane_id: String::new(),
        ..Default::default()
    }
}

fn worker_result_to_proto(
    name: &str,
    result: &crate::services::agent_control::SpawnResult,
) -> exomonad_proto::effects::agent::AgentInfo {
    use crate::services::agent_control::Topology;

    exomonad_proto::effects::agent::AgentInfo {
        id: result.agent_name.to_string(),
        issue: String::new(),
        worktree_path: String::new(),
        branch_name: String::new(),
        agent_type: service_agent_type_to_proto(result.agent_type),
        role: 0,
        alive: true,
        mux_window: result.agent_type.tab_display_name(name),
        error: String::new(),
        pr_number: 0,
        pr_url: String::new(),
        topology: Topology::SharedDir.to_proto(),
        pane_id: result.pane_id.clone().unwrap_or_default(),
        ..Default::default()
    }
}

fn subtree_result_to_proto(
    branch_name: &str,
    result: &crate::services::agent_control::SpawnResult,
) -> EffectResult<exomonad_proto::effects::agent::AgentInfo> {
    use crate::services::agent_control::Topology;
    let actual_branch = spawn_result_branch_name(result)?;

    Ok(exomonad_proto::effects::agent::AgentInfo {
        id: result.agent_name.to_string(),
        issue: String::new(),
        worktree_path: result.worktree_path.display().to_string(),
        branch_name: actual_branch.to_string(),
        agent_type: service_agent_type_to_proto(result.agent_type),
        role: 0,
        alive: true,
        mux_window: result.agent_type.tab_display_name(branch_name),
        error: String::new(),
        pr_number: 0,
        pr_url: String::new(),
        topology: Topology::WorktreePerAgent.to_proto(),
        pane_id: result.pane_id.clone().unwrap_or_default(),
        ..Default::default()
    })
}

fn leaf_subtree_result_to_proto(
    branch_name: &str,
    result: &crate::services::agent_control::SpawnResult,
) -> EffectResult<exomonad_proto::effects::agent::AgentInfo> {
    use crate::services::agent_control::Topology;
    let actual_branch = spawn_result_branch_name(result)?;

    Ok(exomonad_proto::effects::agent::AgentInfo {
        id: result.agent_name.to_string(),
        issue: String::new(),
        worktree_path: result.worktree_path.display().to_string(),
        branch_name: actual_branch.to_string(),
        agent_type: service_agent_type_to_proto(result.agent_type),
        role: 0,
        alive: true,
        mux_window: result.agent_type.tab_display_name(branch_name),
        error: String::new(),
        pr_number: 0,
        pr_url: String::new(),
        topology: Topology::WorktreePerAgent.to_proto(),
        pane_id: result.pane_id.clone().unwrap_or_default(),
        ..Default::default()
    })
}

fn invocation_status_label(status: InvocationStatus) -> &'static str {
    match status {
        InvocationStatus::Running => "running",
        InvocationStatus::Exited => "exited_early",
        InvocationStatus::Failed => "startup_failed",
        InvocationStatus::Killed => "killed",
        InvocationStatus::TimedOut => "timed_out",
    }
}

fn invocation_is_fresh(previous: Option<&InvocationRecord>, current: &InvocationRecord) -> bool {
    previous.is_none_or(|record| record.invocation_id != current.invocation_id)
}

fn invocation_target_label(invocation: &InvocationRecord) -> String {
    if let Some(window_id) = invocation.routing.window_id.as_ref() {
        return format!("window {window_id}");
    }
    if let Some(pane_id) = invocation.routing.pane_id.as_ref() {
        return format!("pane {pane_id}");
    }
    "unresolved target".to_string()
}

fn invocation_handoff_to_proto(
    invocation: &InvocationRecord,
    branch_name: &str,
    fresh: bool,
    ready: bool,
    outcome: &str,
) -> InvocationHandoff {
    let (target_type, target_id) = if let Some(window_id) = invocation.routing.window_id.as_ref() {
        ("window", window_id.to_string())
    } else if let Some(pane_id) = invocation.routing.pane_id.as_ref() {
        ("pane", pane_id.to_string())
    } else {
        ("none", String::new())
    };
    InvocationHandoff {
        invocation_id: invocation.invocation_id.clone(),
        trigger: match invocation.trigger {
            InvocationTrigger::Spawn => "spawn",
            InvocationTrigger::ResumePr => "resume_pr",
            InvocationTrigger::Review => "review",
        }
        .to_string(),
        runtime: invocation.runtime.suffix().to_string(),
        branch_name: branch_name.to_string(),
        target_type: target_type.to_string(),
        target_id,
        fresh,
        ready,
        outcome: outcome.to_string(),
    }
}

fn spawn_result_branch_name(
    result: &crate::services::agent_control::SpawnResult,
) -> EffectResult<&str> {
    if result.branch_name.trim().is_empty() {
        tracing::error!(agent = %result.agent_name, "spawn result did not include the actual git branch");
        return Err(EffectError::invalid_input(
            "spawn result did not include the actual git branch",
        ));
    }
    Ok(&result.branch_name)
}

fn append_spawn_failed<C: HasEventLog>(
    ctx: &Arc<C>,
    parent_agent: &str,
    child_agent: &str,
    intent_id: &str,
    error: &str,
) {
    let Some(log) = ctx.event_log() else {
        return;
    };
    let mut payload = serde_json::json!({
        "child_agent": child_agent,
        "error": error,
        "source": "rust",
    });
    if !intent_id.trim().is_empty() {
        payload["intent_id"] = serde_json::json!(intent_id);
    }
    let _ = log.append("agent.spawn_failed", parent_agent, &payload);
}

fn service_agent_type_to_proto(at: ServiceAgentType) -> i32 {
    match at {
        ServiceAgentType::Claude => AgentType::Claude as i32,
        ServiceAgentType::Shoal => AgentType::Shoal as i32,
        ServiceAgentType::OpenCode => AgentType::Opencode as i32,
        ServiceAgentType::Codex => AgentType::Codex as i32,
        ServiceAgentType::Process => AgentType::Unspecified as i32,
    }
}

pub(crate) struct AgentListMetadata {
    pub(crate) birth_branch: String,
    pub(crate) has_unread: bool,
    pub(crate) last_check_inbox_at: i64,
    pub(crate) last_activity_at: u64,
    pub(crate) is_alive: bool,
    pub(crate) last_known_routing: Option<RoutingInfo>,
    pub(crate) routing_retired: bool,
    pub(crate) routing_exit_code: Option<i32>,
}

#[derive(Debug, Default)]
pub(crate) struct AgentRoutingSnapshot {
    pub(crate) routing: Option<RoutingInfo>,
    pub(crate) retired: bool,
    pub(crate) exit_code: Option<i32>,
}

pub(crate) async fn resolve_agent_liveness(info: &AgentInfo) -> (bool, AgentRoutingSnapshot) {
    let routing_snapshot = read_agent_routing_snapshot(info.agent_dir.as_deref()).await;
    let is_alive = agent_is_alive(info)
        && !routing_snapshot.retired
        && routing_snapshot
            .routing
            .as_ref()
            .is_some_and(RoutingInfo::has_delivery_target);
    (is_alive, routing_snapshot)
}

pub(crate) async fn read_agent_routing_snapshot(agent_dir: Option<&Path>) -> AgentRoutingSnapshot {
    let Some(agent_dir) = agent_dir else {
        return AgentRoutingSnapshot::default();
    };

    let invocation = match read_invocation(agent_dir).await {
        Ok(invocation) => invocation,
        Err(error) => {
            warn!(path = %agent_dir.display(), %error, "Ignoring malformed invocation metadata while listing agents");
            None
        }
    };
    let routing = RoutingInfo::read_from_dir(agent_dir)
        .await
        .ok()
        .or_else(|| invocation.as_ref().map(|record| record.routing.clone()));
    let exit_code = invocation
        .as_ref()
        .and_then(|record| record.exit_code)
        .or_else(|| {
            std::fs::read_to_string(agent_dir.join("exit_code"))
                .ok()
                .and_then(|value| value.trim().parse().ok())
        });
    let retired = agent_dir.join("exited_at").exists()
        || invocation.as_ref().is_some_and(|record| !record.is_live());

    AgentRoutingSnapshot {
        routing,
        retired,
        exit_code,
    }
}

pub(crate) fn service_info_to_proto(
    info: &AgentInfo,
    metadata: AgentListMetadata,
) -> exomonad_proto::effects::agent::AgentInfo {
    let agent_type = match info.agent_type {
        Some(ServiceAgentType::Claude) => AgentType::Claude as i32,
        Some(ServiceAgentType::Shoal) => AgentType::Shoal as i32,
        Some(ServiceAgentType::OpenCode) => AgentType::Opencode as i32,
        Some(ServiceAgentType::Codex) => AgentType::Codex as i32,
        Some(ServiceAgentType::Process) => AgentType::Unspecified as i32,
        None => AgentType::Unspecified as i32,
    };
    let intent_id = info
        .agent_dir
        .as_ref()
        .and_then(|path| std::fs::read_to_string(path.join("dispatch_intent")).ok())
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_default();

    exomonad_proto::effects::agent::AgentInfo {
        id: info.internal_name.to_string(),
        issue: info.internal_name.to_string(),
        worktree_path: info
            .worktree_path
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_default(),
        branch_name: String::new(),
        agent_type,
        role: 0,
        alive: metadata.is_alive,
        mux_window: metadata
            .last_known_routing
            .as_ref()
            .and_then(|routing| routing.window_id.as_ref())
            .map(ToString::to_string)
            .unwrap_or_default(),
        error: if metadata.last_known_routing.is_none() {
            "no routing recorded".to_string()
        } else if metadata.routing_retired {
            match metadata.routing_exit_code {
                Some(exit_code) => format!("retired routing (exit_code={exit_code})"),
                None => "retired routing".to_string(),
            }
        } else {
            String::new()
        },
        pr_number: info.pr.as_ref().map(|p| p.number as i32).unwrap_or(0),
        pr_url: info.pr.as_ref().map(|p| p.url.clone()).unwrap_or_default(),
        topology: info.topology.to_proto(),
        pane_id: metadata
            .last_known_routing
            .as_ref()
            .and_then(|routing| routing.pane_id.as_ref())
            .map(ToString::to_string)
            .unwrap_or_default(),
        birth_branch: metadata.birth_branch,
        has_unread: metadata.has_unread,
        last_check_inbox_at: metadata.last_check_inbox_at,
        is_alive: metadata.is_alive,
        last_activity_at: metadata.last_activity_at as i64,
        intent_id,
    }
}

fn agent_matches_filter(info: &AgentInfo, filter_type: Option<&str>) -> bool {
    let Some(filter_type) = filter_type else {
        return true;
    };

    info.agent_type.is_some_and(|agent_type| {
        agent_type.suffix().eq_ignore_ascii_case(filter_type)
            || format!("{agent_type:?}").eq_ignore_ascii_case(filter_type)
    })
}

fn agent_is_alive(info: &AgentInfo) -> bool {
    let tombstoned = info
        .agent_dir
        .as_ref()
        .is_some_and(|dir| dir.join("exited_at").exists());
    info.has_tab && !tombstoned
}

#[cfg(test)]
mod tests {
    use super::*;
    use exomonad_test_support::init_fixture_git_repository;
    use prost::Message;
    use serial_test::serial;
    use std::os::unix::fs::PermissionsExt;

    fn restart_test_pr() -> ForgejoPullRequest {
        ForgejoPullRequest {
            number: PRNumber::new(7),
            url: "https://forgejo.local/pr/7".to_string(),
            title: "Test PR".to_string(),
            body: String::new(),
            head_ref: BranchName::try_from_str("main.feature-codex")
                .expect("literal branch is non-empty"),
            base_ref: BranchName::try_from_str("main").expect("literal branch is non-empty"),
            state: "open".to_string(),
            merged: false,
            head_sha: Some("abc123".to_string()),
            base_sha: None,
        }
    }

    fn test_handler() -> AgentHandler<crate::services::Services> {
        let services = Arc::new(crate::services::Services::test());
        let service = Arc::new(AgentControlService::new(services.clone()));
        AgentHandler::new(service, services)
    }

    async fn ownership_services(
        publication: &PublishedHead,
        current_invocation_id: &str,
        identity_slice_id: Option<&str>,
    ) -> (tempfile::TempDir, crate::services::Services) {
        let temp_dir = tempfile::tempdir().expect("ownership fixture directory");
        let project_dir = temp_dir.path();
        let agent_dir = project_dir.join(".exo/agents/leaf-codex");
        std::fs::create_dir_all(&agent_dir).expect("create ownership agent directory");
        let identity = AgentIdentityRecord {
            agent_name: AgentName::try_from_str("leaf-codex").expect("agent name"),
            slug: Slug::try_from_str("feat-codex").expect("agent slug"),
            agent_type: ServiceAgentType::Codex,
            birth_branch: BirthBranch::try_from_str("main.feature-codex").expect("birth branch"),
            parent_branch: BirthBranch::try_from_str("main").expect("parent branch"),
            working_dir: PathBuf::from(".exo/worktrees/leaf-codex"),
            display_name: "leaf-codex".to_string(),
            topology: Topology::WorktreePerAgent,
            model: None,
            effort: None,
            ledger_owned: true,
            slice_id: identity_slice_id.map(str::to_owned),
        };
        std::fs::write(
            agent_dir.join("identity.json"),
            serde_json::to_vec_pretty(&identity).expect("serialize identity"),
        )
        .expect("write identity");

        let record = crate::services::agent_control::start_invocation(
            &agent_dir,
            ServiceAgentType::Codex,
            InvocationTrigger::Spawn,
            RoutingInfo::window(
                crate::services::tmux_ipc::WindowId::parse("@42").expect("window id"),
            ),
            Some(publication.pr_number),
            Some(publication.head_sha.clone()),
        )
        .await
        .expect("start fixture invocation");
        let mut record = serde_json::to_value(record).expect("serialize invocation");
        record["invocation_id"] = serde_json::json!(current_invocation_id);
        record["runtime_agent_id"] = serde_json::json!("leaf-codex");
        record["slice_id"] = serde_json::to_value(identity_slice_id).expect("serialize slice");
        record["branch"] = serde_json::json!("main.feature-codex");
        std::fs::write(
            agent_dir.join("invocation.json"),
            serde_json::to_vec_pretty(&record).expect("serialize fixture invocation"),
        )
        .expect("write invocation");

        std::fs::create_dir_all(project_dir.join(".exo")).expect("create project Exo directory");
        std::fs::write(
            project_dir.join(".exo/published-heads.json"),
            serde_json::to_vec_pretty(&serde_json::json!({
                "schema_version": 2,
                "heads": [publication],
            }))
            .expect("serialize publication"),
        )
        .expect("write publication");

        let mut services = crate::services::Services::test();
        services.project_dir = project_dir.to_path_buf();
        services.agent_resolver =
            Arc::new(crate::services::AgentResolver::load(project_dir.to_path_buf()).await);
        (temp_dir, services)
    }

    fn ownership_publication(slice_id: Option<&str>) -> PublishedHead {
        PublishedHead {
            pr_number: 43,
            head_branch: "main.feature-codex".to_string(),
            base_branch: "main".to_string(),
            head_sha: "head-sha".to_string(),
            author_agent: Some("leaf-codex".to_string()),
            author_role: Some("dev".to_string()),
            provenance: PublicationProvenance::LedgerOwned,
            slice_id: slice_id.map(str::to_owned),
            invocation_id: Some("invocation-1".to_string()),
            invocation_trigger: None,
            invocation_runtime: None,
            invocation_succession: Vec::new(),
        }
    }

    #[tokio::test]
    async fn publication_ownership_status_accepts_fanout_current_invocation() {
        let mut publication = ownership_publication(Some("slice-a"));
        for (recorded_at, to_invocation_id) in [
            (1, "invocation-2"),
            (2, "invocation-3"),
            (3, "invocation-4"),
            (4, "invocation-5"),
        ] {
            publication.invocation_succession.push(
                crate::services::pr_registry::InvocationSuccession {
                    from_invocation_id: "invocation-1".to_string(),
                    to_invocation_id: to_invocation_id.to_string(),
                    reason: crate::services::pr_registry::SuccessionReason::SessionRecreate,
                    recorded_at,
                },
            );
        }
        let (_temp_dir, services) =
            ownership_services(&publication, "invocation-5", Some("slice-a")).await;
        assert_eq!(
            publication_ownership_status(
                &services,
                publication.pr_number,
                &publication.head_branch,
                &publication.base_branch,
                &publication.head_sha,
            )
            .await,
            (true, String::new())
        );
    }

    #[tokio::test]
    async fn publication_ownership_status_rejects_unrelated_successor_component() {
        let mut publication = ownership_publication(Some("slice-a"));
        publication.invocation_succession.push(
            crate::services::pr_registry::InvocationSuccession {
                from_invocation_id: "unrelated-root".to_string(),
                to_invocation_id: "invocation-unrelated".to_string(),
                reason: crate::services::pr_registry::SuccessionReason::SessionRecreate,
                recorded_at: 1,
            },
        );
        let (_temp_dir, services) =
            ownership_services(&publication, "invocation-unrelated", Some("slice-a")).await;
        let (verified, error) = publication_ownership_status(
            &services,
            publication.pr_number,
            &publication.head_branch,
            &publication.base_branch,
            &publication.head_sha,
        )
        .await;
        assert!(!verified);
        assert!(error.contains("no recorded succession"));
    }

    #[test]
    fn tl_spawn_preflight_excludes_runtime_state_but_blocks_source_changes() {
        let report = classify_spawn_preflight_entries(
            vec![
                ".chainlink/issues.db".to_string(),
                ".exo/tl-loop/root/run.json".to_string(),
                "src/lib.rs".to_string(),
            ],
            vec![".chainlink/".to_string(), ".exo/".to_string()],
        );

        assert_eq!(
            report.excluded,
            vec![
                ".chainlink/issues.db".to_string(),
                ".exo/tl-loop/root/run.json".to_string()
            ]
        );
        assert_eq!(report.blocking, vec!["src/lib.rs"]);
        let message = dirty_worktree_message(&report.blocking);
        assert!(message.contains("src/lib.rs"));
        assert!(!message.contains("issues.db"));
    }

    #[test]
    fn tl_spawn_preflight_runtime_paths_are_normalized_and_fail_closed() {
        assert_eq!(
            crate::services::normalize_tl_preflight_runtime_path(" ./runtime/state/ "),
            Ok("runtime/state/".to_string())
        );
        assert!(crate::services::normalize_tl_preflight_runtime_path("/absolute/path").is_err());
        assert!(crate::services::normalize_tl_preflight_runtime_path("runtime/../source").is_err());
        assert!(runtime_path_is_excluded(
            "./runtime/state/record.json",
            &["runtime/state/".to_string()]
        ));
    }

    #[tokio::test]
    #[serial]
    async fn spawn_worker_effect_persists_intent_before_tmux_and_emits_ledger_event() {
        let tmux_available = Command::new("tmux")
            .arg("-V")
            .output()
            .await
            .map(|output| output.status.success())
            .unwrap_or(false);
        if !tmux_available {
            return;
        }

        let temp_dir = tempfile::tempdir().expect("temporary project directory");
        let project_dir = temp_dir.path().join("project");
        std::fs::create_dir_all(&project_dir).expect("create fixture project directory");
        init_fixture_git_repository(&project_dir).expect("initialize fixture repository");
        std::fs::write(project_dir.join(".gitignore"), ".exo/\n").expect("write fixture gitignore");
        let git_status = Command::new("git")
            .args(["-C", project_dir.to_str().unwrap(), "add", ".gitignore"])
            .output()
            .await
            .expect("stage fixture gitignore");
        assert!(git_status.status.success());
        let git_commit = Command::new("git")
            .args([
                "-C",
                project_dir.to_str().unwrap(),
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=ExoMonad Test",
                "commit",
                "-m",
                "fixture",
            ])
            .output()
            .await
            .expect("commit fixture gitignore");
        assert!(git_commit.status.success());

        let fake_bin = temp_dir.path().join("fake-bin");
        std::fs::create_dir_all(&fake_bin).expect("create fake runtime directory");
        let marker = project_dir.join("dispatch-marker");
        let fake_codex = fake_bin.join("codex");
        std::fs::write(
            &fake_codex,
            format!(
                "#!/bin/sh\nproject_dir=$(dirname \"$CHAINLINK_DB\")\nintent=\"$project_dir/.exo/agents/worker-codex/dispatch_intent\"\nif [ -f \"$intent\" ]; then printf '%s' \"$intent\" > \"{}\"; else printf '%s' missing > \"{}\"; fi\nsleep 30\n",
                marker.display(),
                marker.display()
            ),
        )
        .expect("write fake runtime");
        std::fs::set_permissions(&fake_codex, std::fs::Permissions::from_mode(0o755))
            .expect("make fake runtime executable");
        let fake_shell = temp_dir.path().join("fake-shell");
        std::fs::write(
            &fake_shell,
            "#!/bin/sh\nif [ \"$1\" = \"-l\" ]; then shift; fi\nif [ \"$1\" = \"-c\" ]; then exec /bin/sh -c \"$2\"; fi\nexec /bin/sh \"$@\"\n",
        )
        .expect("write fake shell");
        std::fs::set_permissions(&fake_shell, std::fs::Permissions::from_mode(0o755))
            .expect("make fake shell executable");

        let old_path = std::env::var_os("PATH");
        let old_shell = std::env::var_os("SHELL");
        let old_tmux_socket = std::env::var_os("EXOMONAD_TMUX_SOCKET");
        std::env::set_var("SHELL", &fake_shell);
        let mut path = std::ffi::OsString::from(&fake_bin);
        path.push(":");
        path.push(old_path.as_deref().unwrap_or_default());
        std::env::set_var("PATH", path);

        let tmux_socket = format!("exo-dispatch-handler-{}", std::process::id());
        let session = format!("exo-dispatch-handler-{}", std::process::id());
        std::env::set_var("EXOMONAD_TMUX_SOCKET", &tmux_socket);
        let session_status = Command::new("tmux")
            .args([
                "-L",
                &tmux_socket,
                "new-session",
                "-d",
                "-s",
                &session,
                "-n",
                "TL",
                "sh",
                "-c",
                "sleep 300",
            ])
            .status()
            .await
            .expect("create test tmux session");
        assert!(session_status.success());
        let path_for_tmux = std::env::var("PATH").expect("test PATH");
        let tmux_path_status = Command::new("tmux")
            .args([
                "-L",
                &tmux_socket,
                "set-environment",
                "-g",
                "PATH",
                &path_for_tmux,
            ])
            .status()
            .await
            .expect("set test tmux PATH");
        assert!(tmux_path_status.success());

        let event_log = Arc::new(
            crate::services::event_log::EventLog::open(project_dir.join(".exo/logs"))
                .expect("open event log"),
        );
        let mut services = crate::services::Services::test();
        services.project_dir = project_dir.to_path_buf();
        services.git_wt = Arc::new(crate::services::git_worktree::GitWorktreeService::new(
            project_dir.to_path_buf(),
        ));
        services.event_log = Some(event_log.clone());
        let services = Arc::new(services);
        let service = Arc::new(
            AgentControlService::new(services.clone())
                .with_tmux_session(session.clone())
                .with_birth_branch(BirthBranch::try_from_str("main").expect("main branch"))
                .with_spawn_agent_type(ServiceAgentType::Codex),
        );
        let handler = AgentHandler::new(service, services);
        let ctx = crate::effects::EffectContext {
            agent_name: AgentName::try_from_str("root").expect("root identity"),
            birth_branch: BirthBranch::try_from_str("main").expect("main branch"),
            working_dir: project_dir.to_path_buf(),
        };
        let request = SpawnWorkerRequest {
            name: "worker".to_string(),
            prompt: "exercise correlated dispatch".to_string(),
            permission_mode: String::new(),
            allowed_tools: Vec::new(),
            disallowed_tools: Vec::new(),
            agent_type: AgentType::Codex as i32,
            intent_id: "intent-live-handler".to_string(),
            model: String::new(),
        };

        let response = handler
            .handle("agent.spawn_worker", &request.encode_to_vec(), &ctx)
            .await
            .expect("spawn effect succeeds");
        let response = SpawnWorkerResponse::decode(response.as_slice()).expect("decode response");
        assert_eq!(
            response.agent.as_ref().map(|agent| agent.id.as_str()),
            Some("worker-codex")
        );

        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
        while !marker.exists() {
            assert!(
                std::time::Instant::now() < deadline,
                "fake runtime did not start"
            );
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }
        assert_eq!(
            std::fs::read_to_string(&marker).expect("read dispatch marker"),
            project_dir
                .join(".exo/agents/worker-codex/dispatch_intent")
                .display()
                .to_string()
        );

        let events = event_log.ledger().read_events().expect("read event ledger");
        let spawned = events
            .iter()
            .find(|entry| entry.event.event_type == "agent.spawned")
            .expect("handler emits authoritative spawn event");
        assert_eq!(spawned.event.data["child_agent"], "worker-codex");
        assert_eq!(spawned.event.data["intent_id"], "intent-live-handler");

        let _ = Command::new("tmux")
            .args(["-L", &tmux_socket, "kill-session", "-t", &session])
            .status()
            .await;
        if let Some(old_path) = old_path {
            std::env::set_var("PATH", old_path);
        } else {
            std::env::remove_var("PATH");
        }
        if let Some(old_shell) = old_shell {
            std::env::set_var("SHELL", old_shell);
        } else {
            std::env::remove_var("SHELL");
        }
        if let Some(old_tmux_socket) = old_tmux_socket {
            std::env::set_var("EXOMONAD_TMUX_SOCKET", old_tmux_socket);
        } else {
            std::env::remove_var("EXOMONAD_TMUX_SOCKET");
        }
    }

    #[test]
    fn test_namespace() {
        let handler = test_handler();
        assert_eq!(handler.namespace(), "agent");
    }

    #[test]
    fn test_convert_agent_type() {
        assert_eq!(
            convert_agent_type(AgentType::Claude).unwrap(),
            ServiceAgentType::Claude
        );
        assert_eq!(
            convert_agent_type(AgentType::Codex).unwrap(),
            ServiceAgentType::Codex
        );
        assert_eq!(
            convert_agent_type(AgentType::Codex).unwrap(),
            ServiceAgentType::Codex
        );
        assert!(convert_agent_type(AgentType::Unspecified).is_err());
    }

    #[test]
    fn referenced_pr_number_requires_an_explicit_pr_marker() {
        assert_eq!(
            referenced_pr_number("Fix PR #104 review comments"),
            Some(104)
        );
        assert_eq!(referenced_pr_number("Repair pull request #7"), Some(7));
        assert_eq!(referenced_pr_number("resume PR#56 after review"), Some(56));
        assert_eq!(referenced_pr_number("Implement issue #104"), None);
        assert_eq!(referenced_pr_number("Fix PR #"), None);
        assert_eq!(referenced_pr_number("Fix PR #104foo"), None);
    }

    #[test]
    fn concise_task_summary_skips_blank_lines_and_bounds_length() {
        assert_eq!(
            concise_task_summary("\n\n  fix the flaky test\nmore"),
            "fix the flaky test"
        );
        assert_eq!(concise_task_summary(""), "");
        let long_line = "x".repeat(500);
        assert_eq!(concise_task_summary(&long_line).chars().count(), 160);
    }

    #[test]
    fn spawned_child_capture_reports_branch_when_present() {
        let capture = spawned_child_capture(
            "leaf-1-codex",
            "codex",
            "main.leaf-1",
            "leaf_subtree",
            Some("gpt-5.6-luna"),
            Some("xhigh"),
            "worktree_per_agent",
        );
        assert_eq!(capture.kind, MemoryKind::SpawnedChild);
        assert!(capture.summary.contains("leaf-1-codex"));
        assert!(capture.summary.contains("main.leaf-1"));
        let metadata = capture.metadata.expect("metadata present");
        assert_eq!(metadata["agent_id"], "leaf-1-codex");
        assert_eq!(metadata["agent_type"], "codex");
        assert_eq!(metadata["branch"], "main.leaf-1");
        assert_eq!(metadata["spawn_type"], "leaf_subtree");
        assert_eq!(metadata["model"], "gpt-5.6-luna");
        assert_eq!(metadata["effort"], "xhigh");
        assert_eq!(metadata["topology"], "worktree_per_agent");
    }

    #[test]
    fn spawned_child_capture_omits_branch_for_workers() {
        let capture = spawned_child_capture(
            "worker-1-codex",
            "codex",
            "",
            "worker",
            None,
            None,
            "shared_dir",
        );
        assert!(capture.summary.contains("worker-1-codex"));
        assert!(!capture.summary.contains("branch"));
        let metadata = capture.metadata.expect("metadata present");
        assert_eq!(metadata["branch"], "");
        assert_eq!(metadata["spawn_type"], "worker");
        assert!(metadata["model"].is_null());
        assert!(metadata["effort"].is_null());
        assert_eq!(metadata["topology"], "shared_dir");
    }

    #[test]
    fn resume_fix_direction_capture_bounds_task_text_and_carries_references() {
        let capture = resume_fix_direction_capture(
            104,
            "abc123",
            "leaf-1-codex",
            "main.leaf-1",
            "address review feedback\nwith extra detail",
            Some("gpt-5.6-luna"),
            Some("xhigh"),
            "worktree_per_agent",
        );
        assert_eq!(capture.kind, MemoryKind::FixDirection);
        assert!(capture.summary.contains("PR #104"));
        assert!(capture.summary.contains("leaf-1-codex"));
        assert!(capture.summary.contains("address review feedback"));
        assert!(!capture.summary.contains("with extra detail"));
        let metadata = capture.metadata.expect("metadata present");
        assert_eq!(metadata["pr_number"], 104);
        assert_eq!(metadata["head_sha"], "abc123");
        assert_eq!(metadata["owner"], "leaf-1-codex");
        assert_eq!(metadata["branch"], "main.leaf-1");
        assert_eq!(metadata["model"], "gpt-5.6-luna");
        assert_eq!(metadata["effort"], "xhigh");
        assert_eq!(metadata["topology"], "worktree_per_agent");
    }

    fn memory_test_context() -> crate::effects::EffectContext {
        crate::effects::EffectContext {
            agent_name: AgentName::try_from_str("leaf-1-codex").expect("literal is valid"),
            birth_branch: BirthBranch::try_from_str("main.leaf-1").expect("literal is valid"),
            working_dir: std::path::PathBuf::from("."),
        }
    }

    #[test]
    fn spawn_and_resume_captures_append_bounded_records_on_success() {
        let handler = test_handler();
        let ctx = memory_test_context();

        let spawned_id = capture_memory(
            &ctx,
            handler.ctx.as_ref(),
            spawned_child_capture(
                "leaf-1-codex",
                "codex",
                "main.leaf-1",
                "leaf_subtree",
                Some("gpt-5.6-luna"),
                Some("xhigh"),
                "worktree_per_agent",
            ),
        );
        assert!(spawned_id.is_some());

        let fix_id = capture_memory(
            &ctx,
            handler.ctx.as_ref(),
            resume_fix_direction_capture(
                104,
                "abc123",
                "leaf-1-codex",
                "main.leaf-1",
                "address review feedback",
                Some("gpt-5.6-luna"),
                Some("xhigh"),
                "worktree_per_agent",
            ),
        );
        assert!(fix_id.is_some());

        let records = handler
            .ctx
            .session_memory()
            .list(crate::services::MemoryFilter::default())
            .expect("list succeeds");
        assert_eq!(records.len(), 2);
        assert!(records.iter().any(|r| r.kind == MemoryKind::SpawnedChild));
        assert!(records.iter().any(|r| r.kind == MemoryKind::FixDirection));
    }

    #[test]
    fn spawn_and_resume_captures_are_fail_open_on_append_failure() {
        let handler = test_handler();
        let ctx = memory_test_context();

        // An out-of-range importance is rejected by the ledger's append
        // validation; capture_memory must swallow that failure and return
        // None rather than surface it to the caller.
        let mut invalid_spawned = spawned_child_capture(
            "leaf-1-codex",
            "codex",
            "main.leaf-1",
            "leaf_subtree",
            Some("gpt-5.6-luna"),
            Some("xhigh"),
            "worktree_per_agent",
        );
        invalid_spawned.importance = 101;
        assert_eq!(
            capture_memory(&ctx, handler.ctx.as_ref(), invalid_spawned),
            None
        );

        let mut invalid_fix = resume_fix_direction_capture(
            104,
            "abc123",
            "leaf-1-codex",
            "main.leaf-1",
            "address review feedback",
            None,
            None,
            "worktree_per_agent",
        );
        invalid_fix.importance = 101;
        assert_eq!(
            capture_memory(&ctx, handler.ctx.as_ref(), invalid_fix),
            None
        );

        let records = handler
            .ctx
            .session_memory()
            .list(crate::services::MemoryFilter::default())
            .expect("list succeeds");
        assert!(records.is_empty());
    }

    fn resume_request() -> SpawnLeafSubtreeRequest {
        SpawnLeafSubtreeRequest {
            task: "address review feedback".to_string(),
            resume_pr_number: 104,
            expected_head_sha: "abc123".to_string(),
            ..Default::default()
        }
    }

    #[test]
    fn resume_request_requires_host_resolved_identity() {
        assert!(validate_resume_request(&resume_request()).is_ok());

        let mut missing_sha = resume_request();
        missing_sha.expected_head_sha.clear();
        assert!(validate_resume_request(&missing_sha).is_err());

        let mut caller_named_branch = resume_request();
        caller_named_branch.branch_name = "invented-fix-opencode".to_string();
        let error = validate_resume_request(&caller_named_branch)
            .unwrap_err()
            .to_string();
        assert!(error.contains("branch_name must be omitted"));

        let mut caller_selected_runtime = resume_request();
        caller_selected_runtime.agent_type = AgentType::Codex as i32;
        assert!(validate_resume_request(&caller_selected_runtime).is_err());
    }

    fn blocked_resume_request() -> SpawnLeafSubtreeRequest {
        SpawnLeafSubtreeRequest {
            task: "continue the blocked task".to_string(),
            blocked_issue_id: 949,
            expected_invocation_id: "invocation-1".to_string(),
            expected_branch: "main.leaf".to_string(),
            expected_worktree_fingerprint: "sha256:abc".to_string(),
            human_approved: true,
            ..Default::default()
        }
    }

    #[test]
    fn blocked_resume_requires_all_host_proofs_and_human_approval() {
        assert!(validate_blocked_resume_request(&blocked_resume_request()).is_ok());

        let mut missing_issue = blocked_resume_request();
        missing_issue.blocked_issue_id = 0;
        assert!(validate_blocked_resume_request(&missing_issue).is_err());

        let mut missing_invocation = blocked_resume_request();
        missing_invocation.expected_invocation_id.clear();
        assert!(validate_blocked_resume_request(&missing_invocation).is_err());

        let mut unapproved = blocked_resume_request();
        unapproved.human_approved = false;
        assert!(validate_blocked_resume_request(&unapproved).is_err());

        let mut caller_selected_identity = blocked_resume_request();
        caller_selected_identity.branch_name = "invented-branch".to_string();
        assert!(validate_blocked_resume_request(&caller_selected_identity).is_err());

        let mut caller_selected_intent = blocked_resume_request();
        caller_selected_intent.intent_id = "caller-intent".to_string();
        let error = validate_blocked_resume_request(&caller_selected_intent)
            .unwrap_err()
            .to_string();
        assert!(error.contains("intent is host-owned"));
    }

    #[test]
    fn resume_identity_accepts_canonical_and_bare_metadata() {
        assert!(leaf_identity_matches(
            "m7-3a-fixture-oracle-opencode",
            "m7-3a-fixture-oracle-opencode"
        ));
        assert!(leaf_identity_matches(
            "m7-3a-fixture-oracle",
            "m7-3a-fixture-oracle-opencode"
        ));
        assert!(!leaf_identity_matches(
            "unrelated-fix-opencode",
            "m7-3a-fixture-oracle-opencode"
        ));
    }

    #[test]
    fn resume_task_composition_preserves_spawn_options_fields() {
        let options = SpawnLeafOptions {
            task: "original task".to_string(),
            branch_name: "owner-slug".to_string(),
            role: Some(crate::domain::Role::dev()),
            agent_type: ServiceAgentType::Codex,
            claude_flags: ClaudeSpawnFlags {
                permission_mode: Some(crate::domain::PermissionMode::Default),
                allowed_tools: vec!["Read".to_string()],
                disallowed_tools: vec!["Bash".to_string()],
            },
            standalone_repo: false,
            allowed_dirs: vec!["docs".to_string()],
            start_point: Some("head-sha".to_string()),
            base_branch: Some("main".to_string()),
            expected_agent_name: Some(
                AgentName::try_from_str("owner-slug-codex").expect("literal is a valid agent name"),
            ),
            invocation_pr_number: Some(104),
            recovery_lineage: None,
            model: None,
        };
        let composed = with_resume_task(options.clone(), Some("continuation"));

        assert_eq!(composed.task, "continuation\n\noriginal task");
        assert_eq!(composed.branch_name, options.branch_name);
        assert_eq!(composed.role, options.role);
        assert_eq!(composed.agent_type, options.agent_type);
        assert_eq!(
            composed.claude_flags.permission_mode,
            options.claude_flags.permission_mode
        );
        assert_eq!(
            composed.claude_flags.allowed_tools,
            options.claude_flags.allowed_tools
        );
        assert_eq!(
            composed.claude_flags.disallowed_tools,
            options.claude_flags.disallowed_tools
        );
        assert_eq!(composed.standalone_repo, options.standalone_repo);
        assert_eq!(composed.allowed_dirs, options.allowed_dirs);
        assert_eq!(composed.start_point, options.start_point);
        assert_eq!(composed.base_branch, options.base_branch);
        assert_eq!(composed.expected_agent_name, options.expected_agent_name);
        assert_eq!(composed.invocation_pr_number, options.invocation_pr_number);
    }

    #[test]
    fn repeated_resume_requires_a_new_invocation_generation() {
        let current = InvocationRecord {
            invocation_id: "invocation-2".to_string(),
            runtime: ServiceAgentType::Codex,
            trigger: InvocationTrigger::ResumePr,
            mode: crate::services::agent_control::InvocationMode::Interactive,
            routing: RoutingInfo::window(
                crate::services::tmux_ipc::WindowId::parse("@43").unwrap(),
            ),
            started_at: 2,
            ended_at: None,
            status: InvocationStatus::Running,
            exit_code: None,
            pr_number: Some(7),
            head_sha: Some("abc123".to_string()),
            model: Some("gpt-5.6-luna".to_string()),
            effort: Some("xhigh".to_string()),
            generation: 2,
            runtime_agent_id: None,
            slice_id: None,
            branch: None,
            worktree: None,
            exit_reason: None,
            exit_classification: None,
            stderr_tail: None,
            prior_invocation_id: None,
            recovery_round: 0,
            authorization_source: None,
        };
        let same_generation = InvocationRecord {
            invocation_id: current.invocation_id.clone(),
            ..current.clone()
        };
        let previous = InvocationRecord {
            invocation_id: "invocation-1".to_string(),
            ..current.clone()
        };

        assert!(!invocation_is_fresh(Some(&same_generation), &current));
        assert!(invocation_is_fresh(Some(&previous), &current));
        assert!(invocation_is_fresh(None, &current));
    }

    #[test]
    fn unspecified_agent_type_uses_configured_spawn_default() {
        assert_eq!(
            convert_agent_type_or_default(AgentType::Unspecified, ServiceAgentType::OpenCode)
                .unwrap(),
            ServiceAgentType::OpenCode
        );
        assert_eq!(
            convert_agent_type_or_default(AgentType::Unspecified, ServiceAgentType::Codex).unwrap(),
            ServiceAgentType::Codex
        );
    }

    #[test]
    fn explicit_agent_type_overrides_configured_spawn_default() {
        assert_eq!(
            convert_agent_type_or_default(AgentType::Claude, ServiceAgentType::OpenCode).unwrap(),
            ServiceAgentType::Claude
        );
    }

    #[test]
    fn same_harness_retry_is_allowed_without_switch_approval() {
        assert_eq!(
            harness_switch_decision(
                ServiceAgentType::OpenCode,
                AgentType::Opencode,
                ServiceAgentType::OpenCode,
                false,
            )
            .expect("same harness should be allowed"),
            None
        );
        assert_eq!(
            harness_switch_decision(
                ServiceAgentType::OpenCode,
                AgentType::Unspecified,
                ServiceAgentType::OpenCode,
                false,
            )
            .expect("configured default should be allowed"),
            None
        );
    }

    #[test]
    fn cross_harness_switch_requires_explicit_approval() {
        let error = harness_switch_decision(
            ServiceAgentType::OpenCode,
            AgentType::Claude,
            ServiceAgentType::Claude,
            false,
        )
        .expect_err("unapproved cross-harness switch must be blocked");
        assert!(error.contains("[STUCK: harness-switch]"));
        assert!(error.contains(HARNESS_SWITCH_APPROVAL_ENV));
    }

    #[test]
    fn approved_cross_harness_switch_has_auditable_policy_source() {
        assert_eq!(
            harness_switch_decision(
                ServiceAgentType::OpenCode,
                AgentType::Claude,
                ServiceAgentType::Claude,
                true,
            )
            .expect("approved switch should be allowed"),
            Some(HARNESS_SWITCH_APPROVAL_ENV)
        );
        let capture = harness_switch_stuck_capture(
            "spawn_worker",
            ServiceAgentType::OpenCode,
            ServiceAgentType::Claude,
            Some("gpt-5.6-luna"),
            Some("xhigh"),
        );
        let metadata = capture.metadata.expect("stuck metadata");
        assert_eq!(metadata["guidance_required"], true);
        assert_eq!(metadata["model"], "gpt-5.6-luna");
        assert_eq!(metadata["effort"], "xhigh");
    }

    #[test]
    fn list_metadata_reports_live_resume_activity_separately_from_inbox_check() {
        let internal_name = AgentName::try_from_str("resume-owner-codex").unwrap();
        let info = AgentInfo {
            internal_name,
            has_tab: true,
            topology: Topology::WorktreePerAgent,
            agent_dir: None,
            worktree_path: None,
            slug: None,
            agent_type: Some(ServiceAgentType::Codex),
            pr: None,
            last_activity_at: Some(1_700_000_000),
        };
        let proto = service_info_to_proto(
            &info,
            AgentListMetadata {
                birth_branch: "main.resume-owner-codex".to_string(),
                has_unread: false,
                last_check_inbox_at: 0,
                last_activity_at: 1_700_000_000,
                is_alive: agent_is_alive(&info),
                last_known_routing: None,
                routing_retired: false,
                routing_exit_code: None,
            },
        );

        assert!(proto.is_alive);
        assert_eq!(proto.last_check_inbox_at, 0);
        assert_eq!(proto.last_activity_at, 1_700_000_000);
    }

    #[tokio::test]
    async fn list_metadata_reports_retired_last_known_routing() {
        let temp_dir = tempfile::tempdir().unwrap();
        let routing =
            RoutingInfo::window(crate::services::tmux_ipc::WindowId::parse("@17").unwrap());
        let agent_dir = temp_dir.path().join("agent");
        let worktree_path = temp_dir.path().join("worktree");
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();
        tokio::fs::create_dir_all(&worktree_path).await.unwrap();
        tokio::fs::write(agent_dir.join("dispatch_intent"), "intent-715\n")
            .await
            .unwrap();
        routing.write_to_dir(&agent_dir).await.unwrap();
        crate::services::agent_control::start_invocation(
            &agent_dir,
            ServiceAgentType::OpenCode,
            InvocationTrigger::ResumePr,
            routing.clone(),
            Some(715),
            Some("abc123".to_string()),
        )
        .await
        .unwrap();
        tokio::fs::remove_file(agent_dir.join("routing.json"))
            .await
            .unwrap();
        finish_invocation_and_tombstone(&agent_dir, &routing, InvocationStatus::Exited, Some(0))
            .await
            .unwrap();

        let snapshot = read_agent_routing_snapshot(Some(&agent_dir)).await;
        assert_eq!(
            snapshot
                .routing
                .as_ref()
                .and_then(|routing| routing.window_id.as_ref())
                .map(ToString::to_string),
            Some("@17".to_string())
        );
        assert!(snapshot.retired);
        assert_eq!(snapshot.exit_code, Some(0));

        let info = AgentInfo {
            internal_name: AgentName::try_from_str("issue-715-opencode").unwrap(),
            has_tab: true,
            topology: Topology::WorktreePerAgent,
            agent_dir: Some(agent_dir),
            worktree_path: Some(worktree_path.clone()),
            slug: None,
            agent_type: Some(ServiceAgentType::OpenCode),
            pr: None,
            last_activity_at: None,
        };
        let (is_alive, _) = resolve_agent_liveness(&info).await;
        assert!(!is_alive);
        let proto = service_info_to_proto(
            &info,
            AgentListMetadata {
                birth_branch: "main.issue-715-opencode".to_string(),
                has_unread: false,
                last_check_inbox_at: 0,
                last_activity_at: 0,
                is_alive: false,
                last_known_routing: snapshot.routing,
                routing_retired: snapshot.retired,
                routing_exit_code: snapshot.exit_code,
            },
        );

        assert_eq!(proto.mux_window, "@17");
        assert_eq!(proto.error, "retired routing (exit_code=0)");
        assert_eq!(proto.intent_id, "intent-715");
        assert_eq!(proto.worktree_path, worktree_path.display().to_string());
        assert!(!proto.is_alive);
    }

    #[tokio::test]
    async fn resolve_agent_liveness_rejects_retired_routing() {
        let temp_dir = tempfile::tempdir().unwrap();
        let routing =
            RoutingInfo::window(crate::services::tmux_ipc::WindowId::parse("@23").unwrap());
        let agent_dir = temp_dir.path().join("retired-agent");
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();
        routing.write_to_dir(&agent_dir).await.unwrap();
        tokio::fs::write(agent_dir.join("exited_at"), "1")
            .await
            .unwrap();

        let info = AgentInfo {
            internal_name: AgentName::try_from_str("retired-opencode").unwrap(),
            has_tab: true,
            topology: Topology::WorktreePerAgent,
            agent_dir: Some(agent_dir),
            worktree_path: None,
            slug: None,
            agent_type: Some(ServiceAgentType::OpenCode),
            pr: None,
            last_activity_at: None,
        };

        let (is_alive, snapshot) = resolve_agent_liveness(&info).await;

        assert!(snapshot.retired);
        assert!(!is_alive);
    }

    #[tokio::test]
    async fn list_metadata_distinguishes_agents_without_routing_history() {
        let temp_dir = tempfile::tempdir().unwrap();
        let snapshot = read_agent_routing_snapshot(Some(temp_dir.path())).await;
        assert!(snapshot.routing.is_none());
        assert!(!snapshot.retired);
        assert_eq!(snapshot.exit_code, None);

        let info = AgentInfo {
            internal_name: AgentName::try_from_str("legacy-opencode").unwrap(),
            has_tab: false,
            topology: Topology::WorktreePerAgent,
            agent_dir: Some(temp_dir.path().to_path_buf()),
            worktree_path: None,
            slug: None,
            agent_type: Some(ServiceAgentType::OpenCode),
            pr: None,
            last_activity_at: None,
        };
        let proto = service_info_to_proto(
            &info,
            AgentListMetadata {
                birth_branch: String::new(),
                has_unread: false,
                last_check_inbox_at: 0,
                last_activity_at: 0,
                is_alive: false,
                last_known_routing: snapshot.routing,
                routing_retired: snapshot.retired,
                routing_exit_code: snapshot.exit_code,
            },
        );

        assert!(proto.mux_window.is_empty());
        assert!(proto.pane_id.is_empty());
        assert_eq!(proto.error, "no routing recorded");
    }

    #[tokio::test]
    async fn test_tombstone_agent_by_pane_retires_routing_and_writes_exited_at() {
        let temp_dir = tempfile::tempdir().unwrap();
        let project_dir = temp_dir.path();
        let agent_dir = project_dir.join(".exo/agents/worker-opencode");
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();
        let pane_id = crate::services::tmux_ipc::PaneId::parse("%42").unwrap();
        RoutingInfo::pane(pane_id, "TL")
            .write_to_dir(&agent_dir)
            .await
            .unwrap();

        assert!(tombstone_agent_by_pane(project_dir, "%42").await);
        assert!(agent_dir.join("exited_at").exists());
        assert!(agent_dir.join("routing.json").exists());
    }

    fn test_forgejo_pr() -> ForgejoPullRequest {
        ForgejoPullRequest {
            number: PRNumber::new(7),
            url: "https://forgejo.local/pr/7".to_string(),
            title: "Test PR".to_string(),
            body: String::new(),
            head_ref: BranchName::try_from_str("main.feature-codex")
                .expect("literal branch is non-empty"),
            base_ref: BranchName::try_from_str("main").expect("literal branch is non-empty"),
            state: "open".to_string(),
            merged: false,
            head_sha: Some("abc123".to_string()),
            base_sha: None,
        }
    }

    #[test]
    fn cleanup_pr_state_refuses_open_pr() {
        let pr = test_forgejo_pr();
        assert_eq!(cleanup_pr_state(&pr).unwrap_err(), "PR #7 is still open");
    }

    #[test]
    fn cleanup_pr_state_accepts_closed_and_merged_prs() {
        let mut pr = test_forgejo_pr();
        pr.state = "closed".to_string();
        assert_eq!(cleanup_pr_state(&pr).unwrap(), "closed");

        pr.state = "open".to_string();
        pr.merged = true;
        assert_eq!(cleanup_pr_state(&pr).unwrap(), "merged");
    }

    fn test_review(state: &str, commit_id: Option<&str>) -> ForgejoPullRequestReview {
        ForgejoPullRequestReview {
            id: None,
            state: state.to_string(),
            body: String::new(),
            commit_id: commit_id.map(str::to_string),
            author_login: None,
            dismissed: false,
            stale: false,
        }
    }

    #[test]
    fn review_authorization_rejects_owner_login() {
        assert!(!review_author_matches_reviewer_login(
            Some("pr-owner"),
            Some("exomonad-reviewer"),
        ));
    }

    #[test]
    fn review_authorization_rejects_unrelated_registered_login() {
        assert!(!review_author_matches_reviewer_login(
            Some("other-registered-agent"),
            Some("exomonad-reviewer"),
        ));
    }

    #[test]
    fn review_authorization_rejects_unregistered_login() {
        assert!(!review_author_matches_reviewer_login(
            Some("unknown-forgejo-login"),
            Some("exomonad-reviewer"),
        ));
    }

    #[test]
    fn review_authorization_accepts_the_reviewer_service_account_login() {
        assert!(review_author_matches_reviewer_login(
            Some("Exomonad-Reviewer"),
            Some("exomonad-reviewer"),
        ));
    }

    #[test]
    fn watcher_pr_review_state_prefers_current_head_changes_requested() {
        let reviews = vec![
            test_review("APPROVED", Some("abc123")),
            test_review("REQUEST_CHANGES", Some("abc123")),
            test_review("APPROVED", Some("oldsha")),
        ];

        let (state, count) = review_state_from_forgejo_reviews(&reviews, "abc123");

        assert_eq!(state, "changes_requested");
        assert_eq!(count, 2);
    }

    #[test]
    fn watcher_pr_review_state_uses_latest_active_current_head_verdict() {
        let reviews = vec![
            test_review("REQUEST_CHANGES", Some("abc123")),
            test_review("APPROVED", Some("abc123")),
        ];

        let (state, count) = review_state_from_forgejo_reviews(&reviews, "abc123");

        assert_eq!(state, "approved");
        assert_eq!(count, 2);
    }

    #[test]
    fn dismissed_current_head_approval_is_not_review_evidence() {
        let mut dismissed = test_review("APPROVED", Some("abc123"));
        dismissed.dismissed = true;
        let active = test_review("APPROVED", Some("abc123"));

        let (state, count) = review_state_from_forgejo_reviews(&[dismissed, active], "abc123");

        assert_eq!(state, "approved");
        assert_eq!(count, 1);
    }

    #[test]
    fn watcher_review_evidence_requires_full_current_head() {
        let reviews = vec![
            test_review("APPROVED", None),
            test_review("APPROVED", Some("oldsha")),
            test_review("APPROVED", Some("abc123")),
            test_review("DISMISSED", Some("abc123")),
        ];

        let latest = latest_exact_forgejo_review(&reviews, "abc123")
            .expect("current-head review should be selected");
        assert_eq!(latest.commit_id.as_deref(), Some("abc123"));
        assert_eq!(latest.state, "APPROVED");
        assert!(latest_exact_forgejo_review(&reviews, "missing").is_none());
        assert_eq!(
            review_state_from_forgejo_reviews(&reviews, "abc123").0,
            "approved"
        );
    }

    #[tokio::test]
    async fn clear_reviewer_review_artifacts_removes_legacy_review_file_and_watcher_state() {
        let dir = tempfile::tempdir().unwrap();
        let reviews = dir.path().join(".exo/reviews");
        std::fs::create_dir_all(&reviews).unwrap();
        std::fs::write(reviews.join("pr_7.json"), "{}").unwrap();

        let state_path = dir.path().join(".exo/watcher-state.json");
        std::fs::write(
            &state_path,
            r#"{"prs":{"7":{"phase":"stuck"},"8":{"phase":"ok"}},"other":true}"#,
        )
        .unwrap();

        clear_reviewer_review_artifacts(dir.path(), 7)
            .await
            .unwrap();

        assert!(!reviews.join("pr_7.json").exists());
        let state: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&state_path).unwrap()).unwrap();
        assert!(state["prs"].get("7").is_none());
        assert!(state["prs"].get("8").is_some());
    }

    #[test]
    fn cleanup_reviewer_leaf_response_fails_when_no_reviewer_window_was_killed() {
        let response = cleanup_reviewer_leaf_response(7, Vec::new(), Ok(()));

        assert!(!response.success);
        assert_eq!(response.pr_number, 7);
        assert!(response.cleaned_reviewers.is_empty());
        assert_eq!(
            response.error,
            "No reviewer tmux windows matched pattern review-pr-7-"
        );
    }

    #[test]
    fn cleanup_reviewer_leaf_response_succeeds_when_reviewer_window_was_killed() {
        let response =
            cleanup_reviewer_leaf_response(7, vec!["review-pr-7-codex".to_string()], Ok(()));

        assert!(response.success);
        assert_eq!(response.error, "");
        assert_eq!(response.cleaned_reviewers, vec!["review-pr-7-codex"]);
    }

    #[tokio::test]
    async fn tombstone_reviewer_agent_dirs_matches_only_requested_pr() {
        let dir = tempfile::tempdir().unwrap();
        let agents_dir = dir.path().join(".exo/agents");
        let matching_agent = agents_dir.join("review-pr-7-codex");
        let forced_matching_agent = agents_dir.join("review-pr-7-123-opencode");
        let other_pr_agent = agents_dir.join("review-pr-70-codex");
        let other_agent = agents_dir.join("feature-codex");

        for agent_dir in [
            &matching_agent,
            &forced_matching_agent,
            &other_pr_agent,
            &other_agent,
        ] {
            tokio::fs::create_dir_all(agent_dir).await.unwrap();
            tokio::fs::write(agent_dir.join("routing.json"), "{}")
                .await
                .unwrap();
        }

        tombstone_reviewer_agent_dirs(dir.path(), 7).await;

        assert!(matching_agent.join("exited_at").exists());
        assert!(matching_agent.join("routing.json").exists());
        assert!(forced_matching_agent.join("exited_at").exists());
        assert!(forced_matching_agent.join("routing.json").exists());
        assert!(!other_pr_agent.join("exited_at").exists());
        assert!(other_pr_agent.join("routing.json").exists());
        assert!(!other_agent.join("exited_at").exists());
        assert!(other_agent.join("routing.json").exists());
    }

    #[tokio::test]
    async fn reset_reviewer_restart_artifacts_resets_persisted_flags() {
        let dir = tempfile::tempdir().unwrap();
        let reviews = dir.path().join(".exo/reviews");
        std::fs::create_dir_all(&reviews).unwrap();
        std::fs::write(reviews.join("pr_7.json"), "{}").unwrap();

        let state_path = dir.path().join(".exo/watcher-state.json");
        std::fs::write(
            &state_path,
            r#"{"prs":{"7":{"rounds":3,"stuck":true,"needs_human_review":true,"last_head_sha":"abc123","last_review_fingerprint":"old"},"8":{"rounds":2,"stuck":true,"needs_human_review":true}}}"#,
        )
        .unwrap();

        let reset = reset_reviewer_restart_artifacts(dir.path(), 7)
            .await
            .unwrap();

        assert_eq!(
            reset,
            RestartReviewArtifactReset {
                watcher_state_found: true,
                legacy_review_file_removed: true,
            }
        );
        assert!(!reviews.join("pr_7.json").exists());
        let state: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&state_path).unwrap()).unwrap();
        assert_eq!(state["prs"]["7"]["rounds"], 0);
        assert_eq!(state["prs"]["7"]["stuck"], false);
        assert_eq!(state["prs"]["7"]["needs_human_review"], false);
        assert_eq!(state["prs"]["7"]["last_head_sha"], "abc123");
        assert!(state["prs"]["7"].get("last_review_fingerprint").is_none());
        assert_eq!(state["prs"]["8"]["rounds"], 2);
    }

    #[test]
    fn restart_review_requires_open_unmerged_pr() {
        let mut closed = restart_test_pr();
        closed.state = "closed".to_string();
        let error = ensure_open_unmerged_pr(&closed, 7).unwrap_err().to_string();
        assert!(error.contains("not open and unmerged"));
        assert!(error.contains("replace_close_pr"));

        let mut merged = restart_test_pr();
        merged.merged = true;
        let error = ensure_open_unmerged_pr(&merged, 7).unwrap_err().to_string();
        assert!(error.contains("not open and unmerged"));

        let open = restart_test_pr();
        assert!(ensure_open_unmerged_pr(&open, 7).is_ok());
    }

    #[test]
    fn replace_pr_accepts_open_and_closed_unmerged_targets() {
        let open = restart_test_pr();
        assert!(ensure_replaceable_unmerged_pr(&open, 7).is_ok());

        let mut merged = restart_test_pr();
        merged.state = "closed".to_string();
        merged.merged = true;
        let error = ensure_replaceable_unmerged_pr(&merged, 7)
            .unwrap_err()
            .to_string();
        assert!(error.contains("merged"));

        let mut closed = restart_test_pr();
        closed.state = "closed".to_string();
        assert!(ensure_replaceable_unmerged_pr(&closed, 7).is_ok());

        let mut unknown = restart_test_pr();
        unknown.state = "draft".to_string();
        let error = ensure_replaceable_unmerged_pr(&unknown, 7)
            .unwrap_err()
            .to_string();
        assert!(error.contains("unsupported state"));
    }

    #[test]
    fn replace_closed_pr_enforces_leaf_identity_and_source_base_separation() {
        assert!(leaf_identity_matches("old-leaf-codex", "old-leaf-codex"));
        assert!(leaf_identity_matches("old-leaf", "old-leaf-codex"));
        assert!(!leaf_identity_matches("other-leaf-codex", "old-leaf-codex"));

        let identity = AgentIdentity::new("fresh-leaf".to_string(), ServiceAgentType::Codex);
        let branch = BirthBranch::try_from_str("release/2026")
            .unwrap()
            .child(identity.internal_name().as_ref());
        assert_eq!(branch.to_string(), "release/2026.fresh-leaf-codex");
        assert_ne!(branch.to_string(), "old-pr-head");
    }

    #[test]
    fn leaf_subtree_response_reports_actual_branch_for_new_and_resumed_leaves() {
        let result = crate::services::agent_control::SpawnResult {
            agent_dir: PathBuf::from(".exo/agents/fix-pr97-ci-codex"),
            worktree_path: PathBuf::from(".exo/worktrees/fix-pr97-ci-codex"),
            branch_name: "main.fix-pr97-ci".to_string(),
            agent_name: AgentName::try_from_str("fix-pr97-ci-codex").unwrap(),
            issue_title: "fix CI".to_string(),
            agent_type: ServiceAgentType::Codex,
            pane_id: None,
        };
        let response = leaf_subtree_result_to_proto("fix-pr97-ci", &result).unwrap();
        assert_eq!(response.branch_name, "main.fix-pr97-ci");
        assert_eq!(response.worktree_path, ".exo/worktrees/fix-pr97-ci-codex");
        let response = subtree_result_to_proto("fix-pr97-ci", &result).unwrap();
        assert_eq!(response.branch_name, "main.fix-pr97-ci");
        assert_eq!(response.worktree_path, ".exo/worktrees/fix-pr97-ci-codex");

        let resumed = crate::services::agent_control::SpawnResult {
            branch_name: "main.rebase-pr95-main-conflicts-opencode".to_string(),
            ..result
        };
        let response =
            leaf_subtree_result_to_proto("rebase-pr95-main-conflicts-opencode", &resumed).unwrap();
        assert_eq!(
            response.branch_name,
            "main.rebase-pr95-main-conflicts-opencode"
        );
    }

    #[test]
    fn branch_response_rejects_missing_actual_branch() {
        let result = crate::services::agent_control::SpawnResult {
            agent_dir: PathBuf::new(),
            worktree_path: PathBuf::new(),
            branch_name: String::new(),
            agent_name: AgentName::try_from_str("missing-branch-codex").unwrap(),
            issue_title: String::new(),
            agent_type: ServiceAgentType::Codex,
            pane_id: None,
        };
        let error = leaf_subtree_result_to_proto("missing-branch", &result).unwrap_err();
        assert!(error.to_string().contains("actual git branch"));
    }

    #[test]
    fn replacement_record_is_retry_safe_and_preserves_failure_details() {
        let mut record = ReplacementRecord::new(
            540,
            7,
            "closed",
            false,
            "main.old-leaf-codex",
            "abc123",
            "main",
            "old-leaf-codex",
            "fresh-leaf",
            "main.fresh-leaf-codex",
            Path::new(".exo/worktrees/fresh-leaf-codex"),
        );
        assert!(record.matches_request(540, 7, "old-leaf-codex", "fresh-leaf", "abc123", "main"));
        record.spawn_status = "spawn_failed".to_string();
        record.error = "tmux unavailable".to_string();
        let response = record.to_response(false);
        assert!(!response.success);
        assert_eq!(response.source_head_sha, "abc123");
        assert_eq!(response.original_base_branch, "main");
        assert!(response.next_action.contains("retry"));
    }

    #[test]
    fn reviewer_window_matches_pr_uses_tmux_reviewer_window_pattern() {
        assert!(reviewer_window_matches_pr("review-pr-7-codex", 7));
        assert!(reviewer_window_matches_pr("2:review-pr-7-123-codex", 7));
        assert!(!reviewer_window_matches_pr("review-pr-70-codex", 7));
        assert!(!reviewer_window_matches_pr("issue-7-codex", 7));
    }

    #[test]
    #[cfg(test)]
    fn cleanup_prs_for_leaf_matches_issue_and_leaf_identity() {
        let mut registry = PrRegistry::default();
        registry.prs.insert(
            3,
            PrEntry {
                number: 3,
                head_branch: "main.feature-codex".to_string(),
                base_branch: "main".to_string(),
                title: String::new(),
                body: String::new(),
                author_agent: "feature-codex".to_string(),
                author_role: "dev".to_string(),
                created_at: chrono::Utc::now(),
                state: PrState::Merged,
                last_review_at: None,
                last_head_sha: None,
                approved_at_sha: None,
                reviewer_agent: None,
                reviewer_birth_branch: None,
                rounds: 0,
                stuck: false,
                needs_human_review: false,
                merge_blocked_on_ci: false,
                chainlink_issue_id: Some(335),
            },
        );

        let prs = cleanup_prs_for_leaf(&registry, 335, "feature-codex");

        assert_eq!(
            prs.iter().map(|(number, _)| *number).collect::<Vec<_>>(),
            vec![3]
        );
    }

    #[test]
    #[cfg(test)]
    fn cleanup_prs_for_leaf_ignores_other_leaf_prs() {
        let mut registry = PrRegistry::default();
        registry.prs.insert(
            4,
            PrEntry {
                number: 4,
                head_branch: "main.other-codex".to_string(),
                base_branch: "main".to_string(),
                title: String::new(),
                body: String::new(),
                author_agent: "other-codex".to_string(),
                author_role: "dev".to_string(),
                created_at: chrono::Utc::now(),
                state: PrState::Open,
                last_review_at: None,
                last_head_sha: None,
                approved_at_sha: None,
                reviewer_agent: None,
                reviewer_birth_branch: None,
                rounds: 0,
                stuck: false,
                needs_human_review: false,
                merge_blocked_on_ci: false,
                chainlink_issue_id: Some(444),
            },
        );

        assert!(cleanup_prs_for_leaf(&registry, 335, "feature-codex").is_empty());
    }
}
