use crate::domain::RoutingInfo;
use crate::services::agent_control::{
    finish_invocation_and_tombstone, read_invocation, InvocationFinishResult, InvocationRecord,
    InvocationStatus,
};
use crate::services::agent_resources::{
    dispose_agent_resources, dispose_exited_reviewer_resources,
};
use crate::services::git_worktree::GitWorktreeService;
use crate::services::EventLog;
use anyhow::{anyhow, Context, Result};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;
use tokio::process::Command;
use tracing::{debug, info, warn};

pub async fn run_orphan_reconciler(
    project_dir: Arc<std::path::PathBuf>,
    git_wt: Arc<GitWorktreeService>,
    interval: Duration,
    max_leaf_session_seconds: u64,
    max_reviewer_session_seconds: u64,
    tmux_session: Option<String>,
    event_log: Option<Arc<EventLog>>,
) {
    let mut ticker = tokio::time::interval(interval);
    loop {
        ticker.tick().await;
        if let Err(err) = reconcile_once_with_event_log(
            &project_dir,
            git_wt.clone(),
            max_leaf_session_seconds,
            max_reviewer_session_seconds,
            tmux_session.as_deref(),
            event_log.as_deref(),
        )
        .await
        {
            warn!(error = %err, "orphan reconciler tick failed");
        }
    }
}

pub async fn reconcile_once(
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
    max_leaf_session_seconds: u64,
    max_reviewer_session_seconds: u64,
    tmux_session: Option<&str>,
) -> Result<()> {
    reconcile_once_with_event_log(
        project_dir,
        git_wt,
        max_leaf_session_seconds,
        max_reviewer_session_seconds,
        tmux_session,
        None,
    )
    .await
}

async fn reconcile_once_with_event_log(
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
    max_leaf_session_seconds: u64,
    max_reviewer_session_seconds: u64,
    tmux_session: Option<&str>,
    event_log: Option<&EventLog>,
) -> Result<()> {
    let disposed_reviewers = dispose_exited_reviewer_resources(project_dir, git_wt.clone()).await;
    if !disposed_reviewers.is_empty() {
        info!(reviewers = ?disposed_reviewers, "Reconciled exited reviewer resources");
    }
    reconcile_issue_worktrees(project_dir, git_wt.clone(), event_log).await?;
    let _ = git_wt;
    reconcile_session_timeouts(
        project_dir,
        max_leaf_session_seconds,
        max_reviewer_session_seconds,
        tmux_session,
    )
    .await?;
    Ok(())
}

async fn reconcile_issue_worktrees(
    project_dir: &Path,
    git_wt: Arc<GitWorktreeService>,
    event_log: Option<&EventLog>,
) -> Result<()> {
    let Some(event_log) = event_log else {
        warn!("Skipping closed-worktree reconciliation because the canonical event log is unavailable");
        return Ok(());
    };
    let worktrees_dir = project_dir.join(".exo/worktrees");
    let Ok(mut entries) = tokio::fs::read_dir(&worktrees_dir).await else {
        return Ok(());
    };

    while let Some(entry) = entries.next_entry().await? {
        if !entry.file_type().await?.is_dir() {
            continue;
        }
        let Some(slug) = entry.file_name().to_str().map(str::to_string) else {
            continue;
        };
        let Some(issue_id) = issue_id_for_worktree(project_dir, &slug).await else {
            continue;
        };
        if issue_closed_event_already_recorded(event_log, issue_id, &slug)? {
            continue;
        }
        if let Some(previous_slug) =
            issue_closed_event_recorded_for_other_worktree(event_log, issue_id, &slug)?
        {
            info!(
                issue_id,
                previous_agent = %previous_slug,
                agent = %slug,
                "Closed issue was already reconciled for a different worktree; checking this worktree independently"
            );
        }
        if chainlink_issue_is_closed(project_dir, issue_id).await? {
            append_issue_closed_event(event_log, issue_id, &slug, "orphan_reconciler")?;
            dispose_agent_resources(project_dir, git_wt.clone(), &slug).await;
            info!(issue_id, agent = %slug, "Reconciled closed Chainlink issue for live worktree");
        }
    }
    Ok(())
}

async fn chainlink_issue_is_closed(project_dir: &Path, issue_id: u64) -> Result<bool> {
    let issue_arg = issue_id.to_string();
    let output = Command::new("chainlink")
        .current_dir(project_dir)
        .args(["show", &issue_arg])
        .output()
        .await
        .with_context(|| format!("failed to run chainlink show {issue_id}"))?;
    if !output.status.success() {
        warn!(
            issue_id,
            stderr = %String::from_utf8_lossy(&output.stderr).trim(),
            "chainlink show failed during orphan reconciliation"
        );
        return Ok(false);
    }
    Ok(chainlink_show_output_is_closed(&String::from_utf8_lossy(
        &output.stdout,
    )))
}

fn chainlink_show_output_is_closed(output: &str) -> bool {
    output
        .lines()
        .any(|line| line.trim().eq_ignore_ascii_case("Status: closed"))
}

fn issue_id_from_slug(slug: &str) -> Option<u64> {
    let rest = slug.strip_prefix("issue-")?;
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        None
    } else {
        digits.parse().ok()
    }
}

async fn issue_id_for_worktree(project_dir: &Path, slug: &str) -> Option<u64> {
    let active_issue_path = project_dir
        .join(".exo/agents")
        .join(slug)
        .join("active_issue");
    match tokio::fs::read_to_string(active_issue_path).await {
        Ok(active_issue) => active_issue
            .trim()
            .parse()
            .ok()
            .or_else(|| issue_id_from_slug(slug)),
        Err(_) => issue_id_from_slug(slug),
    }
}

fn issue_closed_event_already_recorded(
    event_log: &EventLog,
    issue_id: u64,
    slug: &str,
) -> Result<bool> {
    Ok(event_log
        .ledger()
        .read_events()
        .map_err(|error| anyhow!("read canonical issue-close ledger: {error}"))?
        .iter()
        .any(|record| issue_closed_record_matches(&record.event, issue_id, slug)))
}

fn issue_closed_event_recorded_for_other_worktree(
    event_log: &EventLog,
    issue_id: u64,
    slug: &str,
) -> Result<Option<String>> {
    Ok(event_log
        .ledger()
        .read_events()
        .map_err(|error| anyhow!("read canonical issue-close ledger: {error}"))?
        .iter()
        .find_map(|record| {
            let data = record.event.data.as_object()?;
            let recorded_issue_id = data.get("issue_id")?.as_u64()?;
            let recorded_slug = data.get("slug")?.as_str()?;
            (record.event.event_type == "issue.closed"
                && recorded_issue_id == issue_id
                && recorded_slug != slug)
                .then(|| recorded_slug.to_string())
        }))
}

fn issue_closed_record_matches(
    event: &crate::services::LedgerEvent,
    issue_id: u64,
    slug: &str,
) -> bool {
    event.event_type == "issue.closed"
        && event
            .data
            .get("issue_id")
            .and_then(serde_json::Value::as_u64)
            == Some(issue_id)
        && event.data.get("slug").and_then(serde_json::Value::as_str) == Some(slug)
}

fn append_issue_closed_event(
    event_log: &EventLog,
    issue_id: u64,
    slug: &str,
    closed_by: &str,
) -> Result<()> {
    event_log.append(
        "issue.closed",
        slug,
        &serde_json::json!({
            "issue_id": issue_id,
            "slug": slug,
            "closed_by": closed_by,
            "source": "orphan_reconciler",
            "lifecycle_state": "observed",
        }),
    )?;
    Ok(())
}

async fn reconcile_session_timeouts(
    project_dir: &Path,
    max_leaf_session_seconds: u64,
    max_reviewer_session_seconds: u64,
    tmux_session: Option<&str>,
) -> Result<()> {
    if max_leaf_session_seconds == 0 && max_reviewer_session_seconds == 0 {
        return Ok(());
    }

    let agents_dir = project_dir.join(".exo/agents");
    let Ok(mut entries) = tokio::fs::read_dir(&agents_dir).await else {
        return Ok(());
    };

    let now_secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();

    while let Some(entry) = entries.next_entry().await? {
        if !entry.file_type().await?.is_dir() {
            continue;
        }
        let Some(slug) = entry.file_name().to_str().map(str::to_string) else {
            continue;
        };
        if slug == "root" {
            continue;
        }

        let is_reviewer = slug.starts_with("review-pr-");
        let limit = if is_reviewer {
            max_reviewer_session_seconds
        } else {
            max_leaf_session_seconds
        };
        if limit == 0 {
            continue;
        }

        let agent_dir = agents_dir.join(&slug);
        let invocation = match read_invocation(&agent_dir).await {
            Ok(invocation) => invocation,
            Err(error) => {
                warn!(
                    agent = %slug,
                    %error,
                    "Skipping timeout because invocation metadata is malformed"
                );
                continue;
            }
        };
        let routing = match read_effective_routing(&agent_dir, invocation.as_ref()).await {
            Ok(routing) => routing,
            Err(error) => {
                warn!(
                    agent = %slug,
                    %error,
                    "Skipping session timeout because no routing target was recorded"
                );
                continue;
            }
        };
        if agent_dir.join("exited_at").exists() {
            if let Some(session) = tmux_session {
                let _ = kill_agent_window(session, &agent_dir, &slug, &routing).await;
            }
            info!(
                agent = %slug,
                "Retained routing snapshot for exited agent"
            );
            continue;
        }
        if let Some(invocation) = invocation.as_ref() {
            if !invocation.is_live() {
                info!(
                    agent = %slug,
                    invocation_id = %invocation.invocation_id,
                    "Skipping timeout for a dormant invocation-owned worktree"
                );
                continue;
            }
        }
        if !routing.has_delivery_target() {
            warn!(
                agent = %slug,
                "Skipping session timeout because routing has no delivery target"
            );
            continue;
        }
        let spawned_at = match tokio::fs::read_to_string(agent_dir.join("spawned_at")).await {
            Ok(s) => match s.trim().parse::<u64>() {
                Ok(t) => t,
                Err(_) => continue,
            },
            Err(_) => continue,
        };

        let last_activity_at = match tokio::fs::read_to_string(
            agent_dir.join(crate::services::agent_control::LAST_ACTIVITY_FILE),
        )
        .await
        {
            Ok(value) => value.trim().parse::<u64>().ok(),
            Err(_) => None,
        };

        let age_secs = session_age_secs(now_secs, spawned_at, last_activity_at);
        if !timeout_due(age_secs, limit) {
            continue;
        }

        let active_issue = tokio::fs::read_to_string(agent_dir.join("active_issue"))
            .await
            .ok()
            .map(|s| s.trim().to_string());

        let limit_mins = limit / 60;
        let Some(session) = tmux_session else {
            warn!(
                agent = %slug,
                age_secs,
                limit_secs = limit,
                "Skipping session timeout because tmux routing cannot be verified without a session"
            );
            continue;
        };

        let expected_routing = routing;
        match verify_routing_liveness(&expected_routing, invocation.as_ref(), &slug, session).await
        {
            RoutingLiveness::Live => {
                info!(
                    agent = %slug,
                    age_secs,
                    limit_secs = limit,
                    issue = ?active_issue,
                    "Session timeout: killing verified live agent"
                );
                let was_alive =
                    kill_agent_window(session, &agent_dir, &slug, &expected_routing).await;
                match finish_invocation_and_tombstone(
                    &agent_dir,
                    &expected_routing,
                    InvocationStatus::TimedOut,
                    None,
                )
                .await
                {
                    Ok(InvocationFinishResult::IgnoredStale) => {
                        warn!(agent = %slug, "Preserved newer invocation after stale timeout")
                    }
                    Ok(InvocationFinishResult::Finished(_))
                    | Ok(InvocationFinishResult::Missing) => {}
                    Err(error) => warn!(
                        agent = %slug,
                        %error,
                        "Failed to finish timed-out invocation; preserving routing"
                    ),
                }
                notify_tl_about_agent(
                    project_dir,
                    session,
                    &slug,
                    &active_issue,
                    limit_mins,
                    was_alive,
                )
                .await;
            }
            RoutingLiveness::Dead => {
                match finish_invocation_and_tombstone(
                    &agent_dir,
                    &expected_routing,
                    InvocationStatus::Exited,
                    None,
                )
                .await
                {
                    Ok(InvocationFinishResult::IgnoredStale) => {
                        warn!(agent = %slug, "Preserved newer invocation after stale exit")
                    }
                    Ok(InvocationFinishResult::Finished(_))
                    | Ok(InvocationFinishResult::Missing) => {}
                    Err(error) => warn!(
                        agent = %slug,
                        %error,
                        "Failed to finish dead invocation; preserving routing"
                    ),
                }
                info!(
                    agent = %slug,
                    age_secs,
                    limit_secs = limit,
                    "Cleaned stale registry for an already-dead agent"
                );
                notify_tl_about_agent(
                    project_dir,
                    session,
                    &slug,
                    &active_issue,
                    limit_mins,
                    false,
                )
                .await;
            }
            RoutingLiveness::Unverifiable(reason) => {
                warn!(
                    agent = %slug,
                    age_secs,
                    limit_secs = limit,
                    reason,
                    "Skipping session timeout because agent liveness is unverifiable"
                );
            }
        }
    }
    Ok(())
}

#[derive(Debug, PartialEq, Eq)]
enum RoutingLiveness {
    Live,
    Dead,
    Unverifiable(String),
}

fn session_age_secs(now_secs: u64, spawned_at: u64, last_activity_at: Option<u64>) -> u64 {
    let activity_at = last_activity_at.unwrap_or(spawned_at).max(spawned_at);
    now_secs.saturating_sub(activity_at)
}

fn timeout_due(age_secs: u64, limit_secs: u64) -> bool {
    age_secs > limit_secs
}

async fn verify_routing_liveness(
    routing: &RoutingInfo,
    invocation: Option<&InvocationRecord>,
    slug: &str,
    session: &str,
) -> RoutingLiveness {
    if !routing.has_delivery_target() {
        return RoutingLiveness::Unverifiable(
            "routing.json has no window_id or pane_id".to_string(),
        );
    }
    let Some(invocation) = invocation else {
        return RoutingLiveness::Unverifiable(
            "current invocation metadata is unavailable".to_string(),
        );
    };
    if !routing.has_ownership_proof() {
        return RoutingLiveness::Unverifiable(format!(
            "routing ownership proof is incomplete: expected agent={slug} invocation={} generation={}",
            invocation.invocation_id, invocation.generation
        ));
    }
    if routing.owner_agent_id.as_deref() != Some(slug)
        || routing.owner_invocation_id.as_deref() != Some(invocation.invocation_id.as_str())
        || routing.owner_generation != Some(invocation.generation)
    {
        return RoutingLiveness::Unverifiable(format!(
            "routing ownership mismatch: expected agent={slug} invocation={} generation={}, observed agent={:?} invocation={:?} generation={:?}",
            invocation.invocation_id,
            invocation.generation,
            routing.owner_agent_id,
            routing.owner_invocation_id,
            routing.owner_generation
        ));
    }

    let tmux = crate::services::tmux_ipc::TmuxIpc::new(session);
    match tmux.routing_owner(routing).await {
        Ok(Some(owner))
            if owner.agent_id == slug
                && owner.invocation_id == invocation.invocation_id
                && owner.generation == invocation.generation => {}
        Ok(Some(owner)) => {
            return RoutingLiveness::Unverifiable(format!(
                "tmux ownership mismatch: expected agent={slug} invocation={} generation={}, observed agent={} invocation={} generation={}",
                invocation.invocation_id,
                invocation.generation,
                owner.agent_id,
                owner.invocation_id,
                owner.generation
            ));
        }
        Ok(None) => {
            return RoutingLiveness::Unverifiable(
                "tmux target has no complete ownership proof".to_string(),
            );
        }
        Err(error) => return RoutingLiveness::Unverifiable(error.to_string()),
    }
    match crate::services::tmux_ipc::routing_target_alive(routing, &tmux).await {
        Ok(true) => RoutingLiveness::Live,
        Ok(false) => RoutingLiveness::Dead,
        Err(error) => RoutingLiveness::Unverifiable(error.to_string()),
    }
}

async fn read_effective_routing(
    agent_dir: &Path,
    invocation: Option<&InvocationRecord>,
) -> Result<RoutingInfo> {
    match RoutingInfo::read_from_dir(agent_dir).await {
        Ok(routing) => Ok(routing),
        Err(routing_error) => {
            if let Some(invocation) = invocation {
                if invocation.routing.has_delivery_target() {
                    if agent_dir.join("routing.json").exists() {
                        warn!(
                            path = %agent_dir.display(),
                            %routing_error,
                            "Using invocation routing because routing.json is unreadable"
                        );
                    } else {
                        debug!(
                            path = %agent_dir.display(),
                            %routing_error,
                            "Using invocation routing because routing.json is unavailable"
                        );
                    }
                    return Ok(invocation.routing.clone());
                }
            }
            Err(anyhow!(
                "routing.json is unavailable and invocation metadata has no delivery target: {routing_error}"
            ))
        }
    }
}

async fn kill_agent_window(
    session: &str,
    agent_dir: &std::path::Path,
    slug: &str,
    expected_routing: &RoutingInfo,
) -> bool {
    let invocation = match read_invocation(agent_dir).await {
        Ok(invocation) => invocation,
        Err(error) => {
            warn!(agent = %slug, %error, "Could not read invocation for timeout kill (non-fatal)");
            None
        }
    };
    let routing = match read_effective_routing(agent_dir, invocation.as_ref()).await {
        Ok(routing) => routing,
        Err(error) => {
            warn!(agent = %slug, %error, "Could not read routing for timeout kill (non-fatal)");
            return false;
        }
    };
    if !routing.same_delivery_target(expected_routing) {
        warn!(agent = %slug, "Skipping kill for a replaced invocation routing target");
        return false;
    }
    let Some(invocation) = invocation else {
        warn!(agent = %slug, "Skipping kill because current invocation ownership is unavailable");
        return false;
    };
    if routing.owner_agent_id.as_deref() != Some(slug)
        || routing.owner_invocation_id.as_deref() != Some(invocation.invocation_id.as_str())
        || routing.owner_generation != Some(invocation.generation)
    {
        warn!(
            agent = %slug,
            expected_invocation = %invocation.invocation_id,
            expected_generation = invocation.generation,
            observed_agent = ?routing.owner_agent_id,
            observed_invocation = ?routing.owner_invocation_id,
            observed_generation = ?routing.owner_generation,
            "Skipping kill because routing ownership proof does not match"
        );
        return false;
    }

    let tmux = crate::services::tmux_ipc::TmuxIpc::new(session);
    let owner = match tmux.routing_owner(&routing).await {
        Ok(Some(owner)) => owner,
        Ok(None) => {
            warn!(agent = %slug, "Skipping kill because tmux target ownership is unverifiable");
            return false;
        }
        Err(error) => {
            warn!(agent = %slug, %error, "Skipping kill because tmux ownership could not be read");
            return false;
        }
    };
    if owner.agent_id != slug
        || owner.invocation_id != invocation.invocation_id
        || owner.generation != invocation.generation
    {
        warn!(
            agent = %slug,
            expected_invocation = %invocation.invocation_id,
            expected_generation = invocation.generation,
            observed_agent = %owner.agent_id,
            observed_invocation = %owner.invocation_id,
            observed_generation = owner.generation,
            "Skipping kill because tmux target belongs to another invocation"
        );
        return false;
    }

    if let Some(window_id) = &routing.window_id {
        let target = format!("{}:{}", session, window_id.as_str());
        let status = tmux.kill_window(window_id).await;
        match status {
            Ok(()) => {
                info!(agent = %slug, target = %target, "Killed timed-out agent window");
                true
            }
            Err(e) => {
                warn!(agent = %slug, error = %e, "Failed to run tmux kill-window");
                false
            }
        }
    } else if let Some(pane_id) = &routing.pane_id {
        let target = format!("{}:{}", session, pane_id.as_str());
        let status = tmux.kill_pane(pane_id).await;
        match status {
            Ok(()) => {
                info!(agent = %slug, target = %target, "Killed timed-out agent pane");
                true
            }
            Err(e) => {
                warn!(agent = %slug, error = %e, "Failed to run tmux kill-pane");
                false
            }
        }
    } else {
        false
    }
}

async fn notify_tl_about_agent(
    project_dir: &Path,
    session: &str,
    slug: &str,
    active_issue: &Option<String>,
    limit_mins: u64,
    was_alive: bool,
) {
    let root_dir = project_dir.join(".exo/agents/root");
    let Ok(routing) = crate::domain::RoutingInfo::read_from_dir(&root_dir).await else {
        return;
    };
    let Some(window_id) = &routing.window_id else {
        return;
    };

    let message = timeout_notification_message(slug, active_issue, limit_mins, was_alive);

    let target = format!("{}:{}", session, window_id.as_str());
    let tmp = std::env::temp_dir().join(format!("exomonad-timeout-{}.txt", slug));
    if tokio::fs::write(&tmp, &message).await.is_ok() {
        let _ = Command::new("tmux")
            .args(["load-buffer", tmp.to_string_lossy().as_ref()])
            .status()
            .await;
        tokio::time::sleep(Duration::from_millis(50)).await;
        let _ = Command::new("tmux")
            .args(["paste-buffer", "-t", &target])
            .status()
            .await;
        tokio::time::sleep(Duration::from_millis(150)).await;
        let _ = Command::new("tmux")
            .args(["send-keys", "-t", &target, "", "Enter"])
            .status()
            .await;
        let _ = tokio::fs::remove_file(&tmp).await;
    }
}

fn timeout_notification_message(
    slug: &str,
    active_issue: &Option<String>,
    limit_mins: u64,
    was_alive: bool,
) -> String {
    let issue_hint = timeout_issue_hint(active_issue, was_alive);
    if was_alive {
        format!("[TIMED OUT: {slug}] Exceeded {limit_mins}min session limit — killed.{issue_hint}")
    } else {
        format!(
            "[STALE REGISTRY: {slug}] Agent window was already gone — registry entry cleaned up.{issue_hint}"
        )
    }
}

fn timeout_issue_hint(active_issue: &Option<String>, was_alive: bool) -> String {
    match (active_issue, was_alive) {
        (Some(id), true) => {
            format!(" Issue #{id} — call chainlink_timer_stop {id} then re-spec or escalate.")
        }
        (Some(id), false) => {
            format!(
                " Issue #{id} — verify status with chainlink show {id} and re-spec or close if done."
            )
        }
        (None, _) => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timeout_message_reports_live_kill() {
        let message = timeout_notification_message("agent-a", &Some("372".to_string()), 15, true);

        assert!(message.contains("[TIMED OUT: agent-a]"));
        assert!(message.contains("Exceeded 15min session limit"));
        assert!(message.contains("chainlink_timer_stop 372"));
    }

    #[test]
    fn timeout_message_reports_stale_registry() {
        let message = timeout_notification_message("agent-b", &Some("372".to_string()), 15, false);

        assert!(message.contains("[STALE REGISTRY: agent-b]"));
        assert!(message.contains("registry entry cleaned up"));
        assert!(message.contains("chainlink show 372"));
    }

    #[test]
    fn recent_activity_prevents_timeout_from_old_spawn_age() {
        let age = session_age_secs(10_000, 1_000, Some(9_950));

        assert_eq!(age, 50);
        assert!(!timeout_due(age, 60));
        assert!(timeout_due(
            session_age_secs(10_000, 1_000, Some(9_900)),
            60
        ));
    }

    #[test]
    fn parses_issue_id_from_worktree_slug() {
        assert_eq!(
            issue_id_from_slug("issue-313-runtime-hook-codex"),
            Some(313)
        );
        assert_eq!(issue_id_from_slug("review-pr-313-codex"), None);
        assert_eq!(issue_id_from_slug("issue-runtime-hook"), None);
    }

    #[tokio::test]
    async fn active_issue_file_overrides_slug_issue_id() {
        let temp_dir = tempfile::tempdir().unwrap();
        let agent_dir = temp_dir.path().join(".exo/agents/ad-hoc-leaf-opencode");
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();
        tokio::fs::write(agent_dir.join("active_issue"), "313\n")
            .await
            .unwrap();

        assert_eq!(
            issue_id_for_worktree(temp_dir.path(), "ad-hoc-leaf-opencode").await,
            Some(313)
        );
    }

    #[test]
    fn parses_closed_chainlink_show_output() {
        let output = "Issue #313: test\nStatus: closed\nPriority: medium\n";
        assert!(chainlink_show_output_is_closed(output));
        assert!(!chainlink_show_output_is_closed("Status: open\n"));
    }

    #[test]
    fn detects_canonical_issue_closed_records() {
        let event = crate::services::LedgerEvent::new(
            "issue.closed",
            Some("issue-313-first".to_string()),
            serde_json::json!({
                "issue_id": 313,
                "slug": "issue-313-first",
                "closed_by": "test",
            }),
        );
        assert!(issue_closed_record_matches(&event, 313, "issue-313-first"));
        assert!(!issue_closed_record_matches(
            &event,
            313,
            "issue-313-second"
        ));
        assert!(!issue_closed_record_matches(&event, 312, "issue-313-first"));
    }

    #[tokio::test]
    async fn same_issue_worktrees_have_independent_disposal_ledger_entries() {
        let temp_dir = tempfile::tempdir().unwrap();
        let event_log = EventLog::open(temp_dir.path().join(".exo/logs")).unwrap();
        append_issue_closed_event(&event_log, 313, "issue-313-first", "test").unwrap();

        assert!(issue_closed_event_already_recorded(&event_log, 313, "issue-313-first").unwrap());
        assert!(!issue_closed_event_already_recorded(&event_log, 313, "issue-313-second").unwrap());
        assert_eq!(
            issue_closed_event_recorded_for_other_worktree(&event_log, 313, "issue-313-second")
                .unwrap(),
            Some("issue-313-first".to_string())
        );
        assert!(!temp_dir
            .path()
            .join(".exo/events/issue_closed.jsonl")
            .exists());
    }

    #[tokio::test]
    async fn missing_routing_uses_invocation_snapshot_for_timeout_reconciliation() {
        let temp_dir = tempfile::tempdir().unwrap();
        let agent_dir = temp_dir.path().join("agent");
        tokio::fs::create_dir_all(&agent_dir).await.unwrap();
        let routing = RoutingInfo::window(
            crate::services::tmux_ipc::WindowId::parse("@17").expect("valid window id"),
        );
        routing.write_to_dir(&agent_dir).await.unwrap();
        let invocation = crate::services::agent_control::start_invocation(
            &agent_dir,
            crate::services::AgentType::Codex,
            crate::services::agent_control::InvocationTrigger::ResumePr,
            routing.clone(),
            Some(715),
            Some("abc123".to_string()),
        )
        .await
        .unwrap();
        tokio::fs::remove_file(agent_dir.join("routing.json"))
            .await
            .unwrap();

        let effective = read_effective_routing(&agent_dir, Some(&invocation))
            .await
            .unwrap();

        assert_eq!(effective, routing);
    }

    #[tokio::test]
    async fn missing_routing_without_invocation_is_explicitly_unverifiable() {
        let temp_dir = tempfile::tempdir().unwrap();

        let error = read_effective_routing(temp_dir.path(), None)
            .await
            .expect_err("missing routing should not be treated as live");

        assert!(error.to_string().contains("routing.json is unavailable"));
    }
}
