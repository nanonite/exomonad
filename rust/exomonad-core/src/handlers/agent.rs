//! Agent effect handler for the `agent.*` namespace.
//!
//! Uses proto-generated types from `exomonad_proto::effects::agent`.

use crate::domain::{
    AgentName, AgentPermissions, BirthBranch, BranchName, CIStatus, ClaudeSessionUuid, RoutingInfo,
    TeamName,
};
use crate::effects::{
    dispatch_agent_effect, AgentEffects, EffectError, EffectHandler, EffectResult, ResultExt,
    ResultExtPreserve,
};

use super::non_empty;
use crate::services::agent_control::{
    AgentControlService, AgentInfo, AgentType as ServiceAgentType, ClaudeSpawnFlags,
    SpawnGeminiTeammateOptions, SpawnLeafOptions, SpawnOptions, SpawnSubtreeOptions,
    SpawnWorkerOptions,
};
use crate::services::agent_resources::dispose_agent_resources;
use crate::services::forgejo::ForgejoPullRequest;
#[cfg(test)]
use crate::services::pr_registry::PrRegistry;
use crate::services::pr_registry::{LocalReviewState, PrEntry, PrState};
use crate::services::supervisor_registry::SupervisorInfo;
use crate::{GithubOwner, GithubRepo, IssueNumber, PRNumber};
use async_trait::async_trait;
use chrono::Utc;
use exomonad_proto::effects::agent::*;
use std::collections::BTreeSet;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tokio::process::Command;
use tracing::{info, warn};

use crate::services::{
    HasAcpRegistry, HasAgentResolver, HasClaudeSessionRegistry, HasEventLog, HasForgejoClient,
    HasGitHubClient, HasGitWorktreeService, HasProjectDir, HasSupervisorRegistry, HasTeamRegistry,
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
            + HasAcpRegistry
            + HasAgentResolver
            + HasGitHubClient
            + HasProjectDir
            + HasGitWorktreeService
            + HasSupervisorRegistry
            + HasClaudeSessionRegistry
            + HasEventLog
            + HasForgejoClient
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

    async fn ensure_tl_spawn_preflight(
        &self,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<()> {
        let mut failures = Vec::new();

        match dirty_worktree_entries(&ctx.working_dir).await {
            Ok(entries) if entries.is_empty() => {}
            Ok(entries) => failures.push(dirty_worktree_message(&entries)),
            Err(message) => failures.push(format!("worktree check failed: {message}")),
        }

        if let Err(message) = self.check_base_branch_ci(ctx).await {
            failures.push(message);
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
            "TL preflight failed; spawning is blocked until the baseline is fixed or the user acknowledges with EXOMONAD_TL_PREFLIGHT_ACK=1.\n{}",
            failures.join("\n")
        )))
    }

    async fn check_base_branch_ci(
        &self,
        ctx: &crate::effects::EffectContext,
    ) -> Result<(), String> {
        let base_branch = BranchName::try_from_str(ctx.birth_branch.as_parent_branch())
            .map_err(|error| format!("CI check failed: invalid base branch: {error}"))?;

        if !forgejo_workflow_configured(&ctx.working_dir).await {
            info!(
                branch = %base_branch,
                "TL preflight CI check skipped because no .forgejo workflow is configured"
            );
            return Ok(());
        }

        let Some(forgejo) = self.ctx.forgejo_client() else {
            return Err("CI check failed: .forgejo/workflows exists but forgejo_url/forgejo_token are not configured".to_string());
        };

        let repo_info = crate::services::repo::get_repo_info(&ctx.working_dir)
            .await
            .map_err(|error| {
                format!("CI check failed: could not resolve Forgejo repository: {error}")
            })?;
        let status = forgejo
            .latest_actions_status_for_branch(&repo_info.owner, &repo_info.repo, &base_branch)
            .await
            .map_err(|error| format!("CI check failed: Forgejo Actions lookup failed: {error}"))?;

        match status {
            Some(CIStatus::Success | CIStatus::Neutral) => Ok(()),
            Some(CIStatus::Failure) => Err(format!(
                "CI check failed: latest Forgejo Actions run on base branch '{}' is failure",
                base_branch
            )),
            Some(CIStatus::Pending) => Err(format!(
                "CI check failed: latest Forgejo Actions run on base branch '{}' is still pending",
                base_branch
            )),
            Some(CIStatus::Unknown) => Err(format!(
                "CI check failed: latest Forgejo Actions run on base branch '{}' has unknown status",
                base_branch
            )),
            None => Err(format!(
                "CI check failed: .forgejo/workflows exists but no Forgejo Actions run was found for base branch '{}'",
                base_branch
            )),
        }
    }
}

#[async_trait]
impl<
        C: HasTeamRegistry
            + HasAcpRegistry
            + HasAgentResolver
            + HasGitHubClient
            + HasProjectDir
            + HasGitWorktreeService
            + HasSupervisorRegistry
            + HasClaudeSessionRegistry
            + HasEventLog
            + HasForgejoClient
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

async fn dirty_worktree_entries(project_dir: &Path) -> Result<Vec<String>, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(project_dir)
        .args(["status", "--porcelain"])
        .output()
        .await
        .map_err(|error| format!("failed to run git status --porcelain: {error}"))?;

    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }

    Ok(String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::to_string)
        .collect())
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

async fn forgejo_workflow_configured(project_dir: &Path) -> bool {
    let workflows_dir = project_dir.join(".forgejo/workflows");
    let Ok(mut entries) = tokio::fs::read_dir(workflows_dir).await else {
        return false;
    };

    while let Ok(Some(entry)) = entries.next_entry().await {
        let path = entry.path();
        if matches!(
            path.extension().and_then(|ext| ext.to_str()),
            Some("yml" | "yaml")
        ) {
            return true;
        }
    }

    false
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

fn convert_agent_type(t: AgentType) -> EffectResult<ServiceAgentType> {
    match t {
        AgentType::Claude => Ok(ServiceAgentType::Claude),
        AgentType::Gemini => Ok(ServiceAgentType::Gemini),
        AgentType::Shoal => Ok(ServiceAgentType::Shoal),
        AgentType::Opencode => Ok(ServiceAgentType::OpenCode),
        AgentType::Codex => Ok(ServiceAgentType::Codex),
        AgentType::Unspecified => Err(EffectError::invalid_input(
            "agent_type is required (must be 'claude', 'gemini', 'shoal', 'opencode', or 'codex', got UNSPECIFIED)",
        )),
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

#[async_trait]
impl<
        C: HasTeamRegistry
            + HasAcpRegistry
            + HasAgentResolver
            + HasGitHubClient
            + HasProjectDir
            + HasGitWorktreeService
            + HasSupervisorRegistry
            + HasClaudeSessionRegistry
            + HasEventLog
            + HasForgejoClient
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
        let options = SpawnOptions {
            owner: parse_owner(&req.owner)?,
            repo: parse_repo(&req.repo)?,
            agent_type: convert_agent_type(req.agent_type())?,
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
        let agent_type = convert_agent_type(req.agent_type())?;
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

    async fn spawn_gemini_teammate(
        &self,
        req: SpawnGeminiTeammateRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnGeminiTeammateResponse> {
        self.ensure_tl_spawn_preflight(ctx).await?;
        let options = SpawnGeminiTeammateOptions {
            name: AgentName::try_from_str(req.name.as_str())
                .expect("validated string input is non-empty"),
            prompt: req.prompt.clone(),
            agent_type: convert_agent_type(req.agent_type())?,
            subrepo: non_empty(req.subrepo).map(PathBuf::from),
            base_branch: non_empty(req.base_branch).map(|s| {
                BirthBranch::try_from_str(s.as_str()).expect("validated string input is non-empty")
            }),
        };

        let result = self
            .service
            .spawn_gemini_teammate(&options, &ctx.birth_branch)
            .await
            .effect_err_preserve("agent")?;

        Ok(SpawnGeminiTeammateResponse {
            agent: Some(teammate_result_to_proto(&req.name, &result)),
        })
    }

    async fn spawn_worker(
        &self,
        req: SpawnWorkerRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnWorkerResponse> {
        self.ensure_tl_spawn_preflight(ctx).await?;
        let default_type = self.service.default_spawn_agent_type();
        let options = SpawnWorkerOptions {
            name: AgentName::try_from_str(req.name.as_str())
                .expect("validated string input is non-empty"),
            prompt: req.prompt.clone(),
            agent_type: convert_agent_type(req.agent_type()).unwrap_or(default_type),
            claude_flags: claude_spawn_flags(
                req.permission_mode.clone(),
                req.allowed_tools.clone(),
                req.disallowed_tools.clone(),
            ),
        };

        let result = self
            .service
            .spawn_worker(&options, ctx)
            .await
            .effect_err_preserve("agent")?;

        let agent_info = worker_result_to_proto(&req.name, &result);

        tracing::info!(
            otel.name = "agent.spawned",
            child_agent = %agent_info.id,
            agent_type = %AgentType::try_from(agent_info.agent_type).map(|t| format!("{:?}", t)).unwrap_or_else(|_| "unknown".to_string()),
            branch = %agent_info.branch_name,
            spawn_type = "worker",
            "[event] agent.spawned"
        );
        if let Some(log) = self.ctx.event_log() {
            let _ = log.append(
                "agent.spawned",
                ctx.agent_name.as_ref(),
                &serde_json::json!({
                    "child_agent": agent_info.id,
                    "agent_type": format!("{:?}", options.agent_type).to_lowercase(),
                    "spawn_type": "worker",
                    "branch": agent_info.branch_name,
                }),
            );
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
        let options = SpawnSubtreeOptions {
            task: req.task.clone(),
            branch_name: req.branch_name.clone(),
            parent_session_id,
            role: non_empty(req.role.clone()).map(crate::domain::Role::new),
            agent_type: convert_agent_type(req.agent_type()).unwrap_or(default_type),
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
        };

        let result = self
            .service
            .spawn_subtree(&options, &ctx.birth_branch)
            .await
            .effect_err_preserve("agent")?;

        let agent_info = subtree_result_to_proto(&req.branch_name, &result);

        tracing::info!(
            otel.name = "agent.spawned",
            child_agent = %agent_info.id,
            agent_type = %AgentType::try_from(agent_info.agent_type).map(|t| format!("{:?}", t)).unwrap_or_else(|_| "unknown".to_string()),
            branch = %agent_info.branch_name,
            spawn_type = "subtree",
            "[event] agent.spawned"
        );
        if let Some(log) = self.ctx.event_log() {
            let _ = log.append(
                "agent.spawned",
                ctx.agent_name.as_ref(),
                &serde_json::json!({
                    "child_agent": agent_info.id, "agent_type": format!("{:?}", options.agent_type), "spawn_type": "subtree",
                    "branch": agent_info.branch_name,
                }),
            );
        }

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
        let active_reviewer = live_reviewer_for_pr(self.ctx.project_dir(), req.pr_number).await;
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
            cleanup_force_reviewer_resources(&self.service, self.ctx.project_dir(), req.pr_number)
                .await;
        }

        let reviewer_branch = if req.force {
            format!("review-pr-{}-{}", pr.number, Utc::now().timestamp_millis())
        } else {
            format!("review-pr-{}", pr.number)
        };
        let result = self
            .service
            .spawn_reviewer_for_recovery_named(&pr, &ctx.birth_branch, &reviewer_branch)
            .await
            .effect_err_preserve("agent")?;
        let agent_info = subtree_result_to_proto(&reviewer_branch, &result);
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

    async fn spawn_leaf_subtree(
        &self,
        req: SpawnLeafSubtreeRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnLeafSubtreeResponse> {
        self.ensure_tl_spawn_preflight(ctx).await?;
        let default_type = self.service.default_spawn_agent_type();
        let options = SpawnLeafOptions {
            task: req.task.clone(),
            branch_name: req.branch_name.clone(),
            role: non_empty(req.role.clone()).map(crate::domain::Role::new),
            agent_type: convert_agent_type(req.agent_type()).unwrap_or(default_type),
            claude_flags: claude_spawn_flags(
                req.permission_mode.clone(),
                req.allowed_tools.clone(),
                req.disallowed_tools.clone(),
            ),
            standalone_repo: req.standalone_repo,
            allowed_dirs: req.allowed_dirs,
        };

        let result = self
            .service
            .spawn_leaf_subtree(&options, &ctx.birth_branch)
            .await
            .effect_err_preserve("agent")?;

        let agent_info = leaf_subtree_result_to_proto(&req.branch_name, &result);

        tracing::info!(
            otel.name = "agent.spawned",
            child_agent = %agent_info.id,
            agent_type = %AgentType::try_from(agent_info.agent_type).map(|t| format!("{:?}", t)).unwrap_or_else(|_| "unknown".to_string()),
            branch = %agent_info.branch_name,
            spawn_type = "leaf_subtree",
            "[event] agent.spawned"
        );
        if let Some(log) = self.ctx.event_log() {
            let _ = log.append("agent.spawned", ctx.agent_name.as_ref(), &serde_json::json!({
                "child_agent": agent_info.id, "agent_type": format!("{:?}", options.agent_type), "spawn_type": "leaf_subtree",
                "branch": agent_info.branch_name,
            }));
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

        Ok(SpawnLeafSubtreeResponse {
            agent: Some(agent_info),
        })
    }

    async fn spawn_acp(
        &self,
        req: SpawnAcpRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<SpawnAcpResponse> {
        self.ensure_tl_spawn_preflight(ctx).await?;
        let registry = self.ctx.acp_registry();

        // Resolve working directory from context
        let working_dir = ctx.working_dir.clone();

        // Generate MCP settings for the agent using stdio transport
        let agent_name = AgentName::try_from(req.name.clone()).effect_err("agent")?;
        let context_path = self
            .service
            .resolve_role_context(&crate::domain::Role::worker());
        let settings_json = AgentControlService::<C>::generate_gemini_worker_settings(
            agent_name.as_str(),
            context_path.as_deref(),
            &self.service.extra_mcp_servers,
        );

        // Write settings to agent config dir
        let agent_dir = working_dir.join(format!(".exo/agents/{}", agent_name));
        tokio::fs::create_dir_all(&agent_dir)
            .await
            .effect_err("agent")?;
        let settings_path = agent_dir.join("settings.json");
        tokio::fs::write(
            &settings_path,
            serde_json::to_string_pretty(&settings_json).effect_err("agent")?,
        )
        .await
        .effect_err("agent")?;

        info!(
            agent = %agent_name,
            settings = %settings_path.display(),
            "Wrote ACP agent settings"
        );

        let env_vars = vec![
            (
                "GEMINI_CLI_SYSTEM_SETTINGS_PATH".into(),
                settings_path.to_string_lossy().into_owned(),
            ),
            ("EXOMONAD_AGENT_ID".into(), agent_name.to_string()),
        ];

        let conn = crate::services::acp_registry::connect_and_prompt(
            agent_name.clone(),
            "gemini",
            &working_dir,
            &req.prompt,
            env_vars,
        )
        .await
        .effect_err("agent")?;

        registry.register(conn).await;

        info!(agent = %agent_name, "ACP agent spawned and registered");

        let agent_info = exomonad_proto::effects::agent::AgentInfo {
            id: agent_name.to_string(),
            issue: String::new(),
            worktree_path: String::new(),
            branch_name: String::new(),
            agent_type: AgentType::Gemini as i32,
            role: 0,
            alive: true,
            mux_window: String::new(),
            error: String::new(),
            pr_number: 0,
            pr_url: String::new(),
            topology: exomonad_proto::effects::agent::WorkspaceTopology::SharedDir as i32,
            pane_id: String::new(),
        };

        tracing::info!(
            otel.name = "agent.spawned",
            child_agent = %agent_info.id,
            agent_type = %AgentType::try_from(agent_info.agent_type).map(|t| format!("{:?}", t)).unwrap_or_else(|_| "unknown".to_string()),
            branch = %agent_info.branch_name,
            spawn_type = "acp",
            "[event] agent.spawned"
        );
        if let Some(log) = self.ctx.event_log() {
            let _ = log.append(
                "agent.spawned",
                ctx.agent_name.as_ref(),
                &serde_json::json!({
                    "child_agent": agent_info.id, "agent_type": "gemini", "spawn_type": "acp",
                    "branch": agent_info.branch_name,
                }),
            );
        }

        Ok(SpawnAcpResponse {
            agent: Some(agent_info),
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
        let agent_slug = req.agent_slug.trim();
        if agent_slug.is_empty() {
            return Err(EffectError::invalid_input("agent_slug is required"));
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
        _req: ListRequest,
        _ctx: &crate::effects::EffectContext,
    ) -> EffectResult<ListResponse> {
        let infos = self.service.list_agents().await.effect_err("agent")?;

        let agents = infos.iter().map(service_info_to_proto).collect();
        Ok(ListResponse { agents })
    }

    async fn close_self(
        &self,
        _req: CloseSelfRequest,
        ctx: &crate::effects::EffectContext,
    ) -> EffectResult<CloseSelfResponse> {
        let agent_key = ctx.agent_name.to_string();
        let agents_dir = std::path::Path::new(".exo/agents");

        // FIXME: Routing is written under internal_name (slug-suffix, e.g. "beta-gemini")
        // but MCP config passes bare slug as --name (e.g. "beta"). This suffix probing
        // is a band-aid — the real fix is making agent_name consistent between MCP config
        // and routing.json (either always include the suffix, or never).
        let candidates = std::iter::once(agent_key.clone()).chain(
            ["gemini", "claude", "shoal", "opencode"]
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
            Ok(()) => Ok(CloseWorkerPaneResponse {
                success: true,
                error: String::new(),
            }),
            Err(e) => Ok(CloseWorkerPaneResponse {
                success: false,
                error: e.to_string(),
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
            + HasAcpRegistry
            + HasAgentResolver
            + HasGitHubClient
            + HasProjectDir
            + HasGitWorktreeService
            + HasSupervisorRegistry
            + HasClaudeSessionRegistry
            + HasEventLog
            + HasForgejoClient
            + 'static,
    > AgentHandler<C>
{
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
        if pr.merged || !pr.state.eq_ignore_ascii_case("open") {
            return Err(EffectError::invalid_input(format!(
                "PR #{pr_number} is not open and cannot be reviewed"
            )));
        }
        Ok(pr_entry_from_forgejo_pull_request(pr))
    }
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
        review_state: LocalReviewState::PendingReview,
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

async fn live_reviewer_for_pr(project_dir: &Path, pr_number: u64) -> Option<String> {
    let agents_dir = project_dir.join(".exo/agents");
    let reviewer_prefix = format!("review-pr-{pr_number}-");
    let mut entries = match tokio::fs::read_dir(&agents_dir).await {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => return None,
        Err(error) => {
            warn!(path = %agents_dir.display(), %error, "failed to scan reviewer agents");
            return None;
        }
    };

    while let Ok(Some(entry)) = entries.next_entry().await {
        let Some(agent_slug) = entry.file_name().to_str().map(ToOwned::to_owned) else {
            continue;
        };
        if !agent_slug.starts_with(&reviewer_prefix) {
            continue;
        }
        match orphan_agent_window_alive(project_dir, &agent_slug).await {
            Ok(true) => return Some(agent_slug),
            Ok(false) => {}
            Err(error) => warn!(agent = %agent_slug, %error, "failed to check reviewer liveness"),
        }
    }

    None
}

async fn clear_reviewer_review_artifacts(project_dir: &Path, pr_number: u64) -> anyhow::Result<()> {
    remove_legacy_review_file(project_dir, pr_number).await?;
    clear_watcher_pr_state(project_dir, pr_number).await
}

async fn remove_legacy_review_file(project_dir: &Path, pr_number: u64) -> anyhow::Result<()> {
    let review_path = project_dir
        .join(".exo/reviews")
        .join(format!("pr_{pr_number}.json"));
    match tokio::fs::remove_file(&review_path).await {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
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

async fn cleanup_force_reviewer_resources<C>(
    service: &AgentControlService<C>,
    project_dir: &Path,
    pr_number: u64,
) where
    C: HasTeamRegistry
        + HasAcpRegistry
        + HasAgentResolver
        + HasGitHubClient
        + HasProjectDir
        + HasGitWorktreeService
        + 'static,
{
    let slugs = reviewer_resource_slugs_for_pr(project_dir, pr_number).await;
    for slug in slugs {
        if let Err(error) = service.cleanup_agent(&slug).await {
            warn!(reviewer = %slug, %error, "failed to clean reviewer resource during forced respawn");
        }
    }
}

async fn reviewer_resource_slugs_for_pr(project_dir: &Path, pr_number: u64) -> Vec<String> {
    let mut slugs = BTreeSet::new();
    collect_reviewer_resource_slugs(&project_dir.join(".exo/agents"), pr_number, &mut slugs).await;
    collect_reviewer_resource_slugs(&project_dir.join(".exo/worktrees"), pr_number, &mut slugs)
        .await;
    slugs.into_iter().collect()
}

async fn collect_reviewer_resource_slugs(dir: &Path, pr_number: u64, slugs: &mut BTreeSet<String>) {
    let mut entries = match tokio::fs::read_dir(dir).await {
        Ok(entries) => entries,
        Err(error) if error.kind() == ErrorKind::NotFound => return,
        Err(error) => {
            warn!(path = %dir.display(), %error, "failed to scan reviewer resources");
            return;
        }
    };

    while let Ok(Some(entry)) = entries.next_entry().await {
        let Some(name) = entry.file_name().to_str().map(ToOwned::to_owned) else {
            continue;
        };
        if is_reviewer_resource_slug_for_pr(&name, pr_number) {
            slugs.insert(name);
        }
    }
}

fn is_reviewer_resource_slug_for_pr(slug: &str, pr_number: u64) -> bool {
    slug.starts_with(&format!("review-pr-{pr_number}-"))
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
    if let Some(window_id) = &routing.window_id {
        return tmux
            .window_exists(window_id)
            .await
            .map_err(|error| error.to_string());
    }
    if let Some(pane_id) = &routing.pane_id {
        return tmux
            .pane_exists(pane_id)
            .await
            .map_err(|error| error.to_string());
    }
    Ok(false)
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
        worktree_path: result.agent_dir.display().to_string(),
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
    }
}

fn teammate_result_to_proto(
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
        topology: Topology::WorktreePerAgent.to_proto(),
        pane_id: result.pane_id.clone().unwrap_or_default(),
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
    }
}

fn subtree_result_to_proto(
    branch_name: &str,
    result: &crate::services::agent_control::SpawnResult,
) -> exomonad_proto::effects::agent::AgentInfo {
    use crate::services::agent_control::Topology;

    exomonad_proto::effects::agent::AgentInfo {
        id: result.agent_name.to_string(),
        issue: String::new(),
        worktree_path: result.agent_dir.display().to_string(),
        branch_name: branch_name.to_string(),
        agent_type: service_agent_type_to_proto(result.agent_type),
        role: 0,
        alive: true,
        mux_window: result.agent_type.tab_display_name(branch_name),
        error: String::new(),
        pr_number: 0,
        pr_url: String::new(),
        topology: Topology::WorktreePerAgent.to_proto(),
        pane_id: result.pane_id.clone().unwrap_or_default(),
    }
}

fn leaf_subtree_result_to_proto(
    branch_name: &str,
    result: &crate::services::agent_control::SpawnResult,
) -> exomonad_proto::effects::agent::AgentInfo {
    use crate::services::agent_control::Topology;

    exomonad_proto::effects::agent::AgentInfo {
        id: result.agent_name.to_string(),
        issue: String::new(),
        worktree_path: result.agent_dir.display().to_string(),
        branch_name: branch_name.to_string(),
        agent_type: service_agent_type_to_proto(result.agent_type),
        role: 0,
        alive: true,
        mux_window: result.agent_type.tab_display_name(branch_name),
        error: String::new(),
        pr_number: 0,
        pr_url: String::new(),
        topology: Topology::WorktreePerAgent.to_proto(),
        pane_id: result.pane_id.clone().unwrap_or_default(),
    }
}

fn service_agent_type_to_proto(at: ServiceAgentType) -> i32 {
    match at {
        ServiceAgentType::Claude => AgentType::Claude as i32,
        ServiceAgentType::Gemini => AgentType::Gemini as i32,
        ServiceAgentType::Shoal => AgentType::Shoal as i32,
        ServiceAgentType::OpenCode => AgentType::Opencode as i32,
        ServiceAgentType::Codex => AgentType::Codex as i32,
        ServiceAgentType::Process => AgentType::Unspecified as i32,
    }
}

fn service_info_to_proto(info: &AgentInfo) -> exomonad_proto::effects::agent::AgentInfo {
    let agent_type = match info.agent_type {
        Some(ServiceAgentType::Claude) => AgentType::Claude as i32,
        Some(ServiceAgentType::Gemini) => AgentType::Gemini as i32,
        Some(ServiceAgentType::Shoal) => AgentType::Shoal as i32,
        Some(ServiceAgentType::OpenCode) => AgentType::Opencode as i32,
        Some(ServiceAgentType::Codex) => AgentType::Codex as i32,
        Some(ServiceAgentType::Process) => AgentType::Unspecified as i32,
        None => AgentType::Unspecified as i32,
    };

    exomonad_proto::effects::agent::AgentInfo {
        id: info.internal_name.to_string(),
        issue: info.internal_name.to_string(),
        worktree_path: info
            .agent_dir
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_default(),
        branch_name: String::new(),
        agent_type,
        role: 0,
        alive: info.has_tab,
        mux_window: String::new(),
        error: String::new(),
        pr_number: info.pr.as_ref().map(|p| p.number as i32).unwrap_or(0),
        pr_url: info.pr.as_ref().map(|p| p.url.clone()).unwrap_or_default(),
        topology: info.topology.to_proto(),
        pane_id: String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_handler() -> AgentHandler<crate::services::Services> {
        let services = Arc::new(crate::services::Services::test());
        let service = Arc::new(AgentControlService::new(services.clone()));
        AgentHandler::new(service, services)
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
            convert_agent_type(AgentType::Gemini).unwrap(),
            ServiceAgentType::Gemini
        );
        assert_eq!(
            convert_agent_type(AgentType::Codex).unwrap(),
            ServiceAgentType::Codex
        );
        assert!(convert_agent_type(AgentType::Unspecified).is_err());
    }

    #[tokio::test]
    async fn forgejo_workflow_configured_detects_forgejo_yaml() {
        let dir = tempfile::tempdir().unwrap();
        let workflows = dir.path().join(".forgejo/workflows");
        std::fs::create_dir_all(&workflows).unwrap();
        std::fs::write(workflows.join("ci.yml"), "name: CI\n").unwrap();

        assert!(forgejo_workflow_configured(dir.path()).await);
    }

    #[tokio::test]
    async fn forgejo_workflow_configured_ignores_legacy_gitea_yaml() {
        let dir = tempfile::tempdir().unwrap();
        let workflows = dir.path().join(".gitea/workflows");
        std::fs::create_dir_all(&workflows).unwrap();
        std::fs::write(workflows.join("ci.yml"), "name: CI\n").unwrap();

        assert!(!forgejo_workflow_configured(dir.path()).await);
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

    #[tokio::test]
    async fn reviewer_resource_slugs_for_pr_collects_agents_and_worktrees() {
        let dir = tempfile::tempdir().unwrap();
        for path in [
            ".exo/agents/review-pr-7-codex",
            ".exo/worktrees/review-pr-7-codex",
            ".exo/worktrees/review-pr-7-123-codex",
            ".exo/agents/review-pr-70-codex",
            ".exo/worktrees/issue-7-codex",
        ] {
            std::fs::create_dir_all(dir.path().join(path)).unwrap();
        }

        let slugs = reviewer_resource_slugs_for_pr(dir.path(), 7).await;

        assert_eq!(
            slugs,
            vec![
                "review-pr-7-123-codex".to_string(),
                "review-pr-7-codex".to_string()
            ]
        );
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
                review_state: Default::default(),
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
                review_state: Default::default(),
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
