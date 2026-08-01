use crate::domain::RoutingInfo;
use crate::services::agent_control::{
    finish_invocation_and_tombstone, read_invocation, InvocationFinishResult, InvocationStatus,
};
use crate::services::agent_resources::dispose_agent_resources;
use crate::services::git_worktree::GitWorktreeService;
use anyhow::{Context, Result};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;
use tokio::process::Command;
use tracing::{info, warn};

pub async fn run_orphan_reconciler(
    project_dir: Arc<std::path::PathBuf>,
    git_wt: Arc<GitWorktreeService>,
    interval: Duration,
    max_leaf_session_seconds: u64,
    max_reviewer_session_seconds: u64,
    tmux_session: Option<String>,
) {
    let mut ticker = tokio::time::interval(interval);
    loop {
        ticker.tick().await;
        if let Err(err) = reconcile_once(
            &project_dir,
            git_wt.clone(),
            max_leaf_session_seconds,
            max_reviewer_session_seconds,
            tmux_session.as_deref(),
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
    reconcile_issue_worktrees(project_dir, git_wt.clone()).await?;
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
) -> Result<()> {
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
        if issue_closed_event_already_recorded(project_dir, issue_id, &slug).await? {
            continue;
        }
        if let Some(previous_slug) =
            issue_closed_event_recorded_for_other_worktree(project_dir, issue_id, &slug).await?
        {
            info!(
                issue_id,
                previous_agent = %previous_slug,
                agent = %slug,
                "Closed issue was already reconciled for a different worktree; checking this worktree independently"
            );
        }
        if chainlink_issue_is_closed(project_dir, issue_id).await? {
            append_issue_closed_event(project_dir, issue_id, &slug, "orphan_reconciler").await?;
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

async fn issue_closed_event_already_recorded(
    project_dir: &Path,
    issue_id: u64,
    slug: &str,
) -> Result<bool> {
    let path = project_dir.join(".exo/events/issue_closed.jsonl");
    let Ok(content) = tokio::fs::read_to_string(path).await else {
        return Ok(false);
    };
    Ok(content
        .lines()
        .any(|line| issue_closed_line_matches(line, issue_id, slug)))
}

async fn issue_closed_event_recorded_for_other_worktree(
    project_dir: &Path,
    issue_id: u64,
    slug: &str,
) -> Result<Option<String>> {
    let path = project_dir.join(".exo/events/issue_closed.jsonl");
    let Ok(content) = tokio::fs::read_to_string(path).await else {
        return Ok(None);
    };
    Ok(content.lines().find_map(|line| {
        let value = serde_json::from_str::<serde_json::Value>(line).ok()?;
        let payload = value.get("payload")?;
        let recorded_issue_id = payload.get("issue_id")?.as_u64()?;
        let recorded_slug = payload.get("slug")?.as_str()?;
        (recorded_issue_id == issue_id && recorded_slug != slug).then(|| recorded_slug.to_string())
    }))
}

fn issue_closed_line_matches(line: &str, issue_id: u64, slug: &str) -> bool {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
        return false;
    };
    let payload = value.get("payload");
    payload
        .and_then(|payload| payload.get("issue_id"))
        .and_then(serde_json::Value::as_u64)
        == Some(issue_id)
        && payload
            .and_then(|payload| payload.get("slug"))
            .and_then(serde_json::Value::as_str)
            == Some(slug)
}

async fn append_issue_closed_event(
    project_dir: &Path,
    issue_id: u64,
    slug: &str,
    closed_by: &str,
) -> Result<()> {
    let events_dir = project_dir.join(".exo/events");
    tokio::fs::create_dir_all(&events_dir).await?;
    let event = serde_json::json!({
        "event_type": "issue_closed",
        "payload": {
            "issue_id": issue_id,
            "slug": slug,
            "closed_by": closed_by,
        }
    });
    let line = serde_json::to_string(&event)? + "\n";
    let path = events_dir.join("issue_closed.jsonl");
    let mut options = tokio::fs::OpenOptions::new();
    options.create(true).append(true);
    let mut file = options.open(path).await?;
    use tokio::io::AsyncWriteExt;
    file.write_all(line.as_bytes()).await?;
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
        let routing_path = agent_dir.join("routing.json");
        if !routing_path.exists() {
            warn!(
                agent = %slug,
                "Skipping session timeout because routing.json is missing"
            );
            continue;
        }
        if agent_dir.join("exited_at").exists() {
            if let Some(session) = tmux_session {
                let _ = kill_agent_window(session, &agent_dir, &slug, None).await;
            }
            let _ = tokio::fs::remove_file(&routing_path).await;
            info!(agent = %slug, "Cleaned routing for exited agent");
            continue;
        }
        match read_invocation(&agent_dir).await {
            Ok(Some(invocation)) if !invocation.is_live() => {
                info!(
                    agent = %slug,
                    invocation_id = %invocation.invocation_id,
                    "Skipping timeout for a dormant invocation-owned worktree"
                );
                continue;
            }
            Ok(Some(_)) | Ok(None) => {}
            Err(error) => {
                warn!(
                    agent = %slug,
                    %error,
                    "Skipping timeout because invocation metadata is malformed"
                );
                continue;
            }
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

        let expected_routing = RoutingInfo::read_from_dir(&agent_dir).await.ok();
        match verify_routing_liveness(&agent_dir, session).await {
            RoutingLiveness::Live => {
                info!(
                    agent = %slug,
                    age_secs,
                    limit_secs = limit,
                    issue = ?active_issue,
                    "Session timeout: killing verified live agent"
                );
                let was_alive =
                    kill_agent_window(session, &agent_dir, &slug, expected_routing.as_ref()).await;
                if let Some(routing) = expected_routing.as_ref() {
                    match finish_invocation_and_tombstone(
                        &agent_dir,
                        routing,
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
                if let Some(routing) = expected_routing.as_ref() {
                    match finish_invocation_and_tombstone(
                        &agent_dir,
                        routing,
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

async fn verify_routing_liveness(agent_dir: &Path, session: &str) -> RoutingLiveness {
    let routing = match crate::domain::RoutingInfo::read_from_dir(agent_dir).await {
        Ok(routing) => routing,
        Err(error) => return RoutingLiveness::Unverifiable(error.to_string()),
    };
    if !routing.has_delivery_target() {
        return RoutingLiveness::Unverifiable(
            "routing.json has no window_id or pane_id".to_string(),
        );
    }

    let tmux = crate::services::tmux_ipc::TmuxIpc::new(session);
    match crate::services::tmux_ipc::routing_target_alive(&routing, &tmux).await {
        Ok(true) => RoutingLiveness::Live,
        Ok(false) => RoutingLiveness::Dead,
        Err(error) => RoutingLiveness::Unverifiable(error.to_string()),
    }
}

async fn kill_agent_window(
    session: &str,
    agent_dir: &std::path::Path,
    slug: &str,
    expected_routing: Option<&RoutingInfo>,
) -> bool {
    let routing = match crate::domain::RoutingInfo::read_from_dir(agent_dir).await {
        Ok(r) => r,
        Err(e) => {
            warn!(agent = %slug, error = %e, "Could not read routing.json for timeout kill (non-fatal)");
            return false;
        }
    };
    if expected_routing.is_some_and(|expected| expected != &routing) {
        warn!(agent = %slug, "Skipping kill for a replaced invocation routing target");
        return false;
    }

    if let Some(window_id) = &routing.window_id {
        let target = format!("{}:{}", session, window_id.as_str());
        let status = Command::new("tmux")
            .args(["kill-window", "-t", &target])
            .status()
            .await;
        match status {
            Ok(s) if s.success() => {
                info!(agent = %slug, target = %target, "Killed timed-out agent window");
                true
            }
            Ok(s) => {
                warn!(agent = %slug, target = %target, status = ?s, "kill-window returned non-zero (window may already be gone)");
                false
            }
            Err(e) => {
                warn!(agent = %slug, error = %e, "Failed to run tmux kill-window");
                false
            }
        }
    } else if let Some(pane_id) = &routing.pane_id {
        let target = format!("{}:{}", session, pane_id.as_str());
        let status = Command::new("tmux")
            .args(["kill-pane", "-t", &target])
            .status()
            .await;
        match status {
            Ok(s) if s.success() => {
                info!(agent = %slug, target = %target, "Killed timed-out agent pane");
                true
            }
            Ok(s) => {
                warn!(agent = %slug, target = %target, status = ?s, "kill-pane returned non-zero");
                false
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
    fn detects_recorded_issue_closed_lines() {
        let line = r#"{"event_type":"issue_closed","payload":{"issue_id":313,"slug":"issue-313-first","closed_by":"test"}}"#;
        assert!(issue_closed_line_matches(line, 313, "issue-313-first"));
        assert!(!issue_closed_line_matches(line, 313, "issue-313-second"));
        assert!(!issue_closed_line_matches(line, 312, "issue-313-first"));
    }

    #[tokio::test]
    async fn same_issue_worktrees_have_independent_disposal_ledger_entries() {
        let temp_dir = tempfile::tempdir().unwrap();
        append_issue_closed_event(temp_dir.path(), 313, "issue-313-first", "test")
            .await
            .unwrap();

        assert!(
            issue_closed_event_already_recorded(temp_dir.path(), 313, "issue-313-first")
                .await
                .unwrap()
        );
        assert!(
            !issue_closed_event_already_recorded(temp_dir.path(), 313, "issue-313-second")
                .await
                .unwrap()
        );
        assert_eq!(
            issue_closed_event_recorded_for_other_worktree(
                temp_dir.path(),
                313,
                "issue-313-second"
            )
            .await
            .unwrap(),
            Some("issue-313-first".to_string())
        );
    }
}
