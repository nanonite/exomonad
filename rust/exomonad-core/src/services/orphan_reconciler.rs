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
        let Some(issue_id) = issue_id_from_slug(&slug) else {
            continue;
        };
        if issue_closed_event_already_recorded(project_dir, issue_id).await? {
            continue;
        }
        if chainlink_issue_is_closed(project_dir, issue_id).await? {
            append_issue_closed_event(project_dir, issue_id, "orphan_reconciler").await?;
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

async fn issue_closed_event_already_recorded(project_dir: &Path, issue_id: u64) -> Result<bool> {
    let path = project_dir.join(".exo/events/issue_closed.jsonl");
    let Ok(content) = tokio::fs::read_to_string(path).await else {
        return Ok(false);
    };
    Ok(content
        .lines()
        .any(|line| issue_closed_line_matches(line, issue_id)))
}

fn issue_closed_line_matches(line: &str, issue_id: u64) -> bool {
    let Ok(value) = serde_json::from_str::<serde_json::Value>(line) else {
        return false;
    };
    value
        .get("payload")
        .and_then(|payload| payload.get("issue_id"))
        .and_then(serde_json::Value::as_u64)
        == Some(issue_id)
}

async fn append_issue_closed_event(
    project_dir: &Path,
    issue_id: u64,
    closed_by: &str,
) -> Result<()> {
    let events_dir = project_dir.join(".exo/events");
    tokio::fs::create_dir_all(&events_dir).await?;
    let event = serde_json::json!({
        "event_type": "issue_closed",
        "payload": {
            "issue_id": issue_id,
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
            continue;
        }
        if agent_dir.join("exited_at").exists() {
            if let Some(session) = tmux_session {
                let _ = kill_agent_window(session, &agent_dir, &slug).await;
            }
            let _ = tokio::fs::remove_file(&routing_path).await;
            info!(agent = %slug, "Cleaned routing for exited agent");
            continue;
        }
        let spawned_at = match tokio::fs::read_to_string(agent_dir.join("spawned_at")).await {
            Ok(s) => match s.trim().parse::<u64>() {
                Ok(t) => t,
                Err(_) => continue,
            },
            Err(_) => continue,
        };

        let age_secs = now_secs.saturating_sub(spawned_at);
        if age_secs <= limit {
            continue;
        }

        let active_issue = tokio::fs::read_to_string(agent_dir.join("active_issue"))
            .await
            .ok()
            .map(|s| s.trim().to_string());

        let limit_mins = limit / 60;
        info!(
            agent = %slug,
            age_secs,
            limit_secs = limit,
            issue = ?active_issue,
            "Session timeout: killing agent"
        );

        if let Some(session) = tmux_session {
            let was_alive = kill_agent_window(session, &agent_dir, &slug).await;
            let _ = tokio::fs::remove_file(&routing_path).await;
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
    }
    Ok(())
}

async fn kill_agent_window(session: &str, agent_dir: &std::path::Path, slug: &str) -> bool {
    let routing = match crate::domain::RoutingInfo::read_from_dir(agent_dir).await {
        Ok(r) => r,
        Err(e) => {
            warn!(agent = %slug, error = %e, "Could not read routing.json for timeout kill (non-fatal)");
            return false;
        }
    };

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
    fn parses_issue_id_from_worktree_slug() {
        assert_eq!(
            issue_id_from_slug("issue-313-runtime-hook-codex"),
            Some(313)
        );
        assert_eq!(issue_id_from_slug("review-pr-313-codex"), None);
        assert_eq!(issue_id_from_slug("issue-runtime-hook"), None);
    }

    #[test]
    fn parses_closed_chainlink_show_output() {
        let output = "Issue #313: test\nStatus: closed\nPriority: medium\n";
        assert!(chainlink_show_output_is_closed(output));
        assert!(!chainlink_show_output_is_closed("Status: open\n"));
    }

    #[test]
    fn detects_recorded_issue_closed_lines() {
        let line = r#"{"event_type":"issue_closed","payload":{"issue_id":313,"closed_by":"test"}}"#;
        assert!(issue_closed_line_matches(line, 313));
        assert!(!issue_closed_line_matches(line, 312));
    }
}
